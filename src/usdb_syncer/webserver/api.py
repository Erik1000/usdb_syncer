"""JSON REST API for the song collection and download queue."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import flask

from usdb_syncer import SongId, db, utils
from usdb_syncer.song_loader import DownloadManager
from usdb_syncer.usdb_song import UsdbSong, UsdbSongEncoder

if TYPE_CHECKING:
    from collections.abc import Callable


def get_song(song_id: int) -> UsdbSong:
    song = UsdbSong.get(SongId(song_id))
    if song is None:
        flask.abort(404, "Song not found")
    return song


def download_song(song_id: int, *, force_redownload: bool = False) -> UsdbSong:
    song = get_song(song_id)
    if not song.status.can_be_downloaded():
        flask.abort(409, "Song is already queued or downloading")
    if song.sync_meta and song.sync_meta.pinned:
        flask.abort(409, "Pinned songs cannot be downloaded")
    DownloadManager.download(
        [song], utils.ProgressProxy(""), force_redownload=force_redownload
    )
    return song


def _song_json(song: UsdbSong) -> dict[str, Any]:
    status = _status_json(song)
    return {
        **UsdbSongEncoder().default(song),
        **status,
        "is_local": status["is_downloaded"],
        "pinned": bool(song.sync_meta and song.sync_meta.pinned),
    }


def _status_json(song: UsdbSong) -> dict[str, Any]:
    return {
        "song_id": int(song.song_id),
        "status": str(song.status),
        "status_code": int(song.status),
        "is_downloaded": song.is_downloaded(),
    }


def _int64(value: str) -> int:
    number = int(value)
    if not -(2**63) <= number < 2**63:
        flask.abort(400, "Integer filter is outside the supported range")
    return number


def _validate_search() -> None:  # noqa: C901
    args = flask.request.args
    if args.get("sort_by", "artist") not in {"artist", "title", "year", "likes"}:
        flask.abort(400, "sort_by must be artist, title, year or likes")
    if args.get("sort_order", "asc") not in {"asc", "desc"}:
        flask.abort(400, "sort_order must be asc or desc")
    for name, default, minimum, maximum in (
        ("limit", "100", 1, 1000),
        ("offset", "0", 0, 2**31 - 1),
    ):
        try:
            number = int(args.get(name, default))
        except ValueError:
            flask.abort(400, f"{name} must be an integer")
        if not minimum <= number <= maximum:
            flask.abort(400, f"Invalid {name}")
    try:
        for value in args.getlist("status"):
            db.DownloadStatus(int(value))
        for value in args.getlist("year"):
            _int64(value)
        for value in args.getlist("rating"):
            if not math.isfinite(float(value)):
                flask.abort(400, "rating must be a finite number")
        for value in args.getlist("views"):
            lower, upper = value.split("-", maxsplit=1)
            if _int64(lower) < 0 or (upper and _int64(upper) < _int64(lower)):
                flask.abort(400, "Invalid views range")
    except ValueError:
        flask.abort(400, "Invalid status, year, rating or views filter")
    if "golden_notes" in args and args["golden_notes"] not in {"true", "false"}:
        flask.abort(400, "golden_notes must be true or false")


def _force_redownload() -> bool:
    if not flask.request.get_data():
        return False
    if not flask.request.is_json:
        flask.abort(415, "Expected an application/json request body")
    body = flask.request.get_json()
    if not isinstance(body, dict) or body.keys() - {"force_redownload"}:
        flask.abort(400, "Expected an object with an optional force_redownload field")
    force = body.get("force_redownload", False)
    if not isinstance(force, bool):
        flask.abort(400, "force_redownload must be a JSON boolean")
    return force


def create_api(
    get_songs: Callable[[], list[UsdbSong]],
    *,
    show_nonlocal_songs: bool,
    allow_downloading: bool,
) -> flask.Blueprint:
    """Build the API independently of the HTML interface."""
    api = flask.Blueprint("api", __name__, url_prefix="/api")

    @api.get("")
    @api.get("/")
    def info() -> flask.Response:
        return flask.jsonify(
            api_version=1,
            show_nonlocal_songs=show_nonlocal_songs,
            allow_downloading=allow_downloading,
        )

    @api.get("/songs")
    def songs() -> flask.Response:
        _validate_search()
        return flask.jsonify([_song_json(song) for song in get_songs()])

    @api.get("/songs/<int:song_id>")
    def song(song_id: int) -> flask.Response:
        return flask.jsonify(_song_json(get_song(song_id)))

    @api.get("/songs/<int:song_id>/audio")
    def audio(song_id: int) -> flask.Response:
        audio_path = get_song(song_id).audio_path()
        if not audio_path or not audio_path.is_file():
            flask.abort(404, "Audio file not found for this song")
        return flask.send_file(audio_path)

    @api.get("/songs/<int:song_id>/download")
    def download_status(song_id: int) -> flask.Response:
        return flask.jsonify(_status_json(get_song(song_id)))

    @api.post("/songs/<int:song_id>/download")
    def download(song_id: int) -> tuple[flask.Response, int]:
        if not allow_downloading:
            flask.abort(403, "Downloading is not allowed")
        selected = download_song(song_id, force_redownload=_force_redownload())
        response = flask.jsonify(_status_json(selected))
        response.headers["Location"] = flask.url_for(
            ".download_status", song_id=song_id
        )
        return response, 202

    return api
