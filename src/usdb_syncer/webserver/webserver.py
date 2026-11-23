"""General webserver functions."""

from __future__ import annotations

import base64
import errno
import functools
import itertools
import logging
import os
import socket
import sys
import uuid
from collections import Counter
from io import BytesIO
from typing import Any, ClassVar

import flask
import qrcode
import werkzeug.serving
from PySide6 import QtCore
from werkzeug.exceptions import HTTPException
from werkzeug.serving import WSGIRequestHandler

from usdb_syncer import SongId, db, errors, logger, utils
from usdb_syncer.usdb_song import UsdbSong
from usdb_syncer.webserver import api

_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def _parse_view_parmeter(param: str) -> tuple[int, int | None]:
    lower, upper = param.split("-", maxsplit=1)
    return int(lower), int(upper) if upper else None


def _filter_allowed_statuses(
    requested: list[db.DownloadStatus], show_nonlocal_songs: bool
) -> list[db.DownloadStatus]:
    if show_nonlocal_songs:
        return requested
    local = db.DownloadStatus.local()
    return [s for s in requested if s in local] or local


def _parse_bool_parameter(param: str) -> bool:
    match param:
        case "true":
            return True
        case "false":
            return False
        case _:
            raise ValueError


def _parse_search(
    request: flask.Request, show_nonlocal_songs: bool
) -> db.SearchBuilder:
    builder = db.SearchBuilder()
    if search := request.args.get("search"):
        builder.text = search
    sort_by = request.args.get("sort_by", "artist")
    match sort_by:
        case "artist":
            builder.order = db.SongOrder.ARTIST
        case "title":
            builder.order = db.SongOrder.TITLE
        case "year":
            builder.order = db.SongOrder.YEAR
    sort_order = request.args.get("sort_order", "asc")
    if sort_order == "desc":
        builder.descending = True
    builder.artists = request.args.getlist("artist")
    builder.titles = request.args.getlist("title")
    builder.editions = request.args.getlist("edition")
    builder.ratings = request.args.getlist("rating", float)
    builder.statuses = _filter_allowed_statuses(
        request.args.getlist("status", lambda s: db.DownloadStatus(int(s))),
        show_nonlocal_songs,
    )
    builder.languages = request.args.getlist("language")
    builder.views = request.args.getlist("views", _parse_view_parmeter)
    builder.years = request.args.getlist("year", int)
    builder.genres = request.args.getlist("genre")
    builder.creators = request.args.getlist("creator")
    builder.golden_notes = request.args.get("golden_notes", type=_parse_bool_parameter)
    return builder


def _get_songs(
    request: flask.Request, show_nonlocal_songs: bool, like_counter: Counter[SongId]
) -> list[UsdbSong]:
    builder = _parse_search(request, show_nonlocal_songs)
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    song_ids = db.search_usdb_songs(builder)
    if request.args.get("sort_by") == "likes":
        song_ids = sorted(
            song_ids,
            key=lambda s: like_counter[s],
            reverse=request.args.get("sort_order") == "desc",
        )
    songs = (
        song
        for song_id in song_ids
        if (song := UsdbSong.get(song_id))
        and (show_nonlocal_songs or song.is_downloaded())
    )
    return list(itertools.islice(songs, offset, offset + limit))


def _index(
    title: str,
    show_nonlocal_songs: bool,
    allow_downloading: bool,
    like_counter: Counter[SongId],
    session_id: str,
) -> str:
    songs = _get_songs(flask.request, show_nonlocal_songs, like_counter)
    search = flask.request.args.get("search", default="")
    sort_by = flask.request.args.get("sort_by", "artist")
    sort_order = flask.request.args.get("sort_order", "asc")
    offset = flask.request.args.get("offset", 0, type=int)
    if "HX-Request" in flask.request.headers:
        return flask.render_template(
            "songs_table.html",
            songs=songs,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            offset=offset,
            allow_downloading=allow_downloading,
            liked_songs=_get_liked_songs(session_id),
            like_counter=like_counter,
        )
    return flask.render_template(
        "index.html",
        title=title,
        qrcode=base64.b64encode(get_qrcode(address())).decode("utf-8"),
        songs=songs,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        offset=offset,
        allow_downloading=allow_downloading,
        liked_songs=_get_liked_songs(session_id),
        like_counter=like_counter,
    )


def _fragments_songs(
    show_nonlocal_songs: bool,
    allow_downloading: bool,
    like_counter: Counter[SongId],
    session_id: str,
) -> str:
    songs = _get_songs(flask.request, show_nonlocal_songs, like_counter)
    search = flask.request.args.get("search", default="")
    sort_by = flask.request.args.get("sort_by", "artist")
    sort_order = flask.request.args.get("sort_order", "asc")
    offset = flask.request.args.get("offset", 0, type=int)
    return flask.render_template(
        "songs_rows.html",
        songs=songs,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        offset=offset,
        allow_downloading=allow_downloading,
        liked_songs=_get_liked_songs(session_id),
        like_counter=like_counter,
    )


def _download(song_id: int, start: bool) -> UsdbSong:
    if not start:
        return api.get_song(song_id)
    force = flask.request.args.get("force_redownload", "false")
    if force not in {"true", "false"}:
        flask.abort(400, "force_redownload must be true or false")
    return api.download_song(song_id, force_redownload=force == "true")


def _get_liked_songs(session_id: str) -> set[SongId]:
    cookie = flask.request.cookies.get("liked_songs", "")
    if not cookie.startswith(session_id + ":") or len(cookie) <= len(session_id) + 1:
        return set()
    return set(map(SongId.parse, cookie[len(session_id) + 1 :].split(",")))


def _fragments_like(
    session_id: str, like_counter: Counter[SongId], selected_id: int, like: bool
) -> flask.Response:
    song_id = SongId(selected_id)
    liked_songs = _get_liked_songs(session_id)
    if like and song_id not in liked_songs:
        liked_songs.add(song_id)
        like_counter[song_id] += 1
    elif not like and song_id in liked_songs:
        liked_songs.remove(song_id)
        like_counter[song_id] -= 1
    resp = flask.make_response(
        flask.render_template(
            "like_button.html",
            song_id=song_id,
            like_counter=like_counter,
            liked_songs=liked_songs,
        )
    )
    resp.set_cookie(
        "liked_songs",
        f"{session_id}:{','.join(map(str, liked_songs))}",
        max_age=_COOKIE_MAX_AGE,
    )
    return resp


def create_app(  # noqa: C901
    title: str, *, show_nonlocal_songs: bool, allow_downloading: bool
) -> flask.Flask:
    """Create the browser interface and REST API with shared server capabilities."""
    app = flask.Flask(__name__)
    like_counter: Counter[SongId] = Counter()
    session_id = str(uuid.uuid4())
    app.register_blueprint(
        api.create_api(
            lambda: _get_songs(flask.request, show_nonlocal_songs, like_counter),
            show_nonlocal_songs=show_nonlocal_songs,
            allow_downloading=allow_downloading,
        )
    )

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> flask.Response:
        response = app.make_response(error.get_response())
        if flask.request.path == "/api" or flask.request.path.startswith("/api/"):
            response.data = app.json.dumps(
                {"error": {"code": error.code, "message": error.description}}
            )
            response.content_type = "application/json"
        return response

    @app.route("/")
    def index() -> str:
        return _index(
            title, show_nonlocal_songs, allow_downloading, like_counter, session_id
        )

    @app.route("/fragments/songs")
    def fragments_songs() -> str:
        return _fragments_songs(
            show_nonlocal_songs, allow_downloading, like_counter, session_id
        )

    @app.route("/fragments/songs/<int:selected_id>/download")
    def fragments_get_download(selected_id: int) -> str:
        if not allow_downloading:
            return flask.abort(403, "Downloading is not allowed")
        song = _download(selected_id, start=False)
        return flask.render_template(
            "play_or_download_button.html",
            allow_downloading=allow_downloading,
            song=song,
        )

    @app.post("/fragments/songs/<int:selected_id>/download")
    def fragments_post_download(selected_id: int) -> str:
        if not allow_downloading:
            return flask.abort(403, "Downloading is not allowed")
        song = _download(selected_id, start=True)
        return flask.render_template(
            "play_or_download_button.html",
            allow_downloading=allow_downloading,
            song=song,
        )

    @app.put("/fragments/songs/<int:selected_id>/like")
    def fragments_like(selected_id: int) -> flask.Response:
        return _fragments_like(session_id, like_counter, selected_id, like=True)

    @app.delete("/fragments/songs/<int:selected_id>/like")
    def fragments_unlike(selected_id: int) -> flask.Response:
        return _fragments_like(session_id, like_counter, selected_id, like=False)

    @app.before_request
    def before_request() -> None:
        db.connect(utils.AppPaths.db)

    @app.teardown_request
    def teardown_request(exception: Any = None) -> None:  # noqa: ARG001
        db.close()

    return app


# Keep the former private factory available to existing integrations.
_create_app = create_app


class _CustomRequestHandler(WSGIRequestHandler):
    def log(self, type: str, message: str, *args: Any) -> None:  # noqa: A002, ARG002
        # don't log requests to info level
        super().log("debug", message, *args)


class _WebserverThread(QtCore.QThread):
    def __init__(self, server: werkzeug.serving.BaseWSGIServer) -> None:
        super().__init__()
        self.server = server

    def run(self) -> None:
        self.server.serve_forever()


DEFAULT_TITLE = "USDB Syncer Song Collection"


class _WebserverManager:
    _server: ClassVar[werkzeug.serving.BaseWSGIServer | None] = None
    _thread: ClassVar[QtCore.QThread | None] = None
    host: ClassVar[str] = ""
    port: ClassVar[int] = 0
    title: ClassVar[str] = DEFAULT_TITLE

    @classmethod
    def start(
        cls,
        host: str | None = None,
        port: int | None = None,
        title: str | None = None,
        show_nonlocal_songs: bool = False,
        allow_downloading: bool = False,
    ) -> bool:
        if cls._server:
            return False
        if title:
            cls.title = title
        app = create_app(
            cls.title,
            show_nonlocal_songs=show_nonlocal_songs,
            allow_downloading=allow_downloading,
        )
        cls.host = (host or "").strip() or get_local_ip()
        cls._validate_port(port)
        try:
            cls._server = werkzeug.serving.make_server(
                cls.host, cls.port, app, request_handler=_CustomRequestHandler
            )
        except (OSError, SystemExit) as error:
            # Werkzeug exits on bind failure; report it to the GUI/CLI instead.
            raise errors.ServerBindError(cls.host, cls.port) from error
        cls.port = cls._server.socket.getsockname()[1]
        cls._thread = _WebserverThread(cls._server)
        cls._thread.start()
        return True

    @classmethod
    def stop(cls) -> bool:
        if not cls._server or not cls._thread:
            return False
        cls._server.shutdown()
        cls._server.server_close()
        cls._server = None
        cls._thread.quit()
        cls._thread.wait()
        cls._thread = None
        return True

    @classmethod
    def is_running(cls) -> bool:
        return bool(cls._server)

    @classmethod
    def _validate_port(cls, port: int | None) -> None:
        """Validate and return a usable TCP port.

        - If port is None, return 0 (OS auto-assign).
        - On Unix-like OS (Linux, macOS), disallow ports <1024 (privileged).
        - On Windows, all ports >=1 are allowed.
        - Ensures port <= 65535.
        - Raises PortInUseError if the chosen port is busy.
        """
        if port is None or port == 0:
            cls.port = 0  # OS auto-assign
            return

        if not (0 < port < 65536):
            raise errors.InvalidPortError(port)

        if sys.platform != "win32" and port < 1024:
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                raise errors.PrivilegedPortError(port)

        try:
            family, socktype, proto, _, sockaddr = socket.getaddrinfo(
                cls.host,
                port,
                family=socket.AF_INET6 if ":" in cls.host else socket.AF_INET,
                type=socket.SOCK_STREAM,
            )[0]
            with socket.socket(family, socktype, proto) as s:
                if sys.platform != "win32":
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(sockaddr)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                raise errors.PortInUseError(port, cls.host) from e
            raise errors.ServerBindError(cls.host, port) from e

        cls.port = port


def start(
    host: str | None = None,
    port: int | None = None,
    title: str | None = None,
    show_nonlocal_songs: bool = False,
    allow_downloading: bool = False,
) -> None:
    logging.getLogger("werkzeug").setLevel(logging.DEBUG)
    if _WebserverManager.start(
        host=host,
        port=port,
        title=title,
        show_nonlocal_songs=show_nonlocal_songs,
        allow_downloading=allow_downloading,
    ):
        logger.logger.info(f"Webserver is now running on {address()}")


def stop() -> None:
    if _WebserverManager.stop():
        logger.logger.info("Webserver was stopped.")


def is_running() -> bool:
    return _WebserverManager.is_running()


def address() -> str:
    host = _WebserverManager.host
    if host == "0.0.0.0":  # noqa: S104 - display a usable URL for wildcard binds
        host = get_local_ip()
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{_WebserverManager.port}"


@functools.lru_cache(maxsize=1)
def get_qrcode(data: str) -> bytes:
    buf = BytesIO()
    qrcode.make(data, box_size=5, border=2).save(buf)
    return buf.getvalue()


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    try:
        sock.connect(("10.255.255.255", 1))
        ip = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return ip
