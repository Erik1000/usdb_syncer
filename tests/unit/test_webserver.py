"""Webserver search, REST API, and socket lifecycle regressions."""

from __future__ import annotations

import json
import socket
import sys
from collections import Counter
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import urlencode

import attrs
import flask
import pytest
import requests

from usdb_syncer import SongId, db, errors
from usdb_syncer.webserver import api, webserver

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask.testing import FlaskClient

    from usdb_syncer.usdb_song import UsdbSong


LOCAL = db.DownloadStatus.local()
NONLOCAL = [s for s in db.DownloadStatus if s not in LOCAL]


@pytest.fixture
def app() -> flask.Flask:
    return flask.Flask(__name__)


def _parse(
    app: flask.Flask, params: list[tuple[str, object]], show_nonlocal: bool
) -> db.SearchBuilder:
    query = urlencode(params, doseq=True)
    with app.test_request_context(f"/api/songs?{query}"):
        return webserver._parse_search(flask.request, show_nonlocal)


def test_parse_search_sets_all_parameters(app: flask.Flask) -> None:
    params = [
        ("search", "Rock"),
        ("sort_by", "year"),
        ("sort_order", "desc"),
        ("artist", "Queen"),
        ("artist", "Earth, Wind & Fire"),  # comma must survive
        ("title", "Bohemian Rhapsody"),
        ("title", "Dancing Queen"),
        ("edition", "Deluxe"),
        ("edition", "Remaster"),
        ("rating", "4.5"),
        ("rating", "3"),
        ("status", db.DownloadStatus.OUTDATED.value),
        ("status", db.DownloadStatus.SYNCHRONIZED.value),
        ("language", "en"),
        ("language", "de"),
        ("views", "1-5"),
        ("views", "10-"),
        ("year", "1975"),
        ("year", "1980"),
        ("genre", "Rock"),
        ("genre", "Pop"),
        ("creator", "alice"),
        ("creator", "bob"),
        ("golden_notes", "true"),
    ]

    builder = _parse(app, params, show_nonlocal=True)

    assert builder.text == "Rock"
    assert builder.order == db.SongOrder.YEAR
    assert builder.descending is True
    assert builder.artists == ["Queen", "Earth, Wind & Fire"]
    assert builder.titles == ["Bohemian Rhapsody", "Dancing Queen"]
    assert builder.editions == ["Deluxe", "Remaster"]
    assert builder.ratings == [4.5, 3.0]
    assert builder.statuses == [
        db.DownloadStatus.OUTDATED,
        db.DownloadStatus.SYNCHRONIZED,
    ]
    assert builder.languages == ["en", "de"]
    assert builder.views == [(1, 5), (10, None)]
    assert builder.years == [1975, 1980]
    assert builder.genres == ["Rock", "Pop"]
    assert builder.creators == ["alice", "bob"]
    assert builder.golden_notes is True


@pytest.mark.parametrize(
    ("sort_by", "expected"),
    [
        ("artist", db.SongOrder.ARTIST),
        ("title", db.SongOrder.TITLE),
        ("year", db.SongOrder.YEAR),
    ],
)
def test_parse_search_sort_by(
    app: flask.Flask, sort_by: str, expected: db.SongOrder
) -> None:
    builder = _parse(app, [("sort_by", sort_by)], show_nonlocal=True)
    assert builder.order == expected


def _status_params(statuses: list[db.DownloadStatus]) -> list[tuple[str, object]]:
    return [("status", s.value) for s in statuses]


def test_parse_search_hides_nonlocal_statuses(app: flask.Flask) -> None:
    requested = LOCAL + NONLOCAL

    builder = _parse(app, _status_params(requested), show_nonlocal=False)

    assert builder.statuses == LOCAL


def test_parse_search_keeps_requested_local_subset(app: flask.Flask) -> None:
    builder = _parse(
        app,
        _status_params([db.DownloadStatus.SYNCHRONIZED, *NONLOCAL]),
        show_nonlocal=False,
    )

    assert builder.statuses == [db.DownloadStatus.SYNCHRONIZED]


def test_parse_search_only_nonlocal_requested_falls_back_to_local(
    app: flask.Flask,
) -> None:
    builder = _parse(app, _status_params(NONLOCAL), show_nonlocal=False)

    assert builder.statuses == LOCAL


def test_parse_search_no_statuses_requested_defaults_to_local(app: flask.Flask) -> None:
    builder = _parse(app, [], show_nonlocal=False)

    assert builder.statuses == LOCAL


def test_parse_search_shows_nonlocal_statuses_when_enabled(app: flask.Flask) -> None:
    requested = NONLOCAL + LOCAL

    builder = _parse(app, _status_params(requested), show_nonlocal=True)

    assert builder.statuses == requested


def test_api_songs_returns_json_for_usdb_song(song: UsdbSong) -> None:
    song.sync_meta = None
    app = webserver._create_app(
        "test", show_nonlocal_songs=True, allow_downloading=False
    )
    app.config["TESTING"] = True

    # Skip the real DB handshake; this test only covers JSON encoding.
    with (
        patch.object(webserver.db, "connect"),
        patch.object(webserver.db, "close"),
        patch.object(webserver, "_get_songs", return_value=[song]),
    ):
        client: FlaskClient = app.test_client()
        response = client.get("/api/songs?search=ABBA&limit=100&offset=0")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = json.loads(response.get_data(as_text=True))
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["artist"] == song.artist
    assert payload[0]["title"] == song.title
    assert payload[0]["song_id"] == int(song.song_id)


@pytest.fixture
def api_client(downloaded_song: UsdbSong) -> Iterator[FlaskClient]:
    app = webserver.create_app("test", show_nonlocal_songs=True, allow_downloading=True)
    app.testing = True
    with (
        patch.object(webserver.db, "connect"),
        patch.object(webserver.db, "close"),
        patch.object(api.UsdbSong, "get", return_value=downloaded_song),
    ):
        yield app.test_client()


def test_api_info_and_song(api_client: FlaskClient, song: UsdbSong) -> None:
    assert api_client.get("/api").json == {
        "api_version": 1,
        "allow_downloading": True,
        "show_nonlocal_songs": True,
    }
    response = api_client.get(f"/api/songs/{song.song_id}")
    assert response.get_json()["artist"] == song.artist
    assert response.get_json()["status_code"] == int(song.status)
    assert response.get_json()["is_local"] is True
    assert response.get_json()["is_downloaded"] is True
    assert "sync_meta" not in response.get_json()


@pytest.mark.parametrize("force", [False, True])
def test_api_download_forwards_force_option(
    api_client: FlaskClient, song: UsdbSong, force: bool
) -> None:
    def queue(*args: object, **kwargs: object) -> None:
        song.status = db.DownloadStatus.PENDING

    with patch.object(api.DownloadManager, "download", side_effect=queue) as download:
        response = api_client.post(
            f"/api/songs/{song.song_id}/download", json={"force_redownload": force}
        )
    assert response.status_code == 202
    assert response.get_json()["status"] == "Pending"
    assert download.call_args.kwargs == {"force_redownload": force}
    assert download.call_args.args[0] == [song]
    assert (
        api_client.get(response.headers["Location"]).get_json()["status"] == "Pending"
    )


def test_api_download_accepts_empty_body(api_client: FlaskClient) -> None:
    with patch.object(api.DownloadManager, "download") as download:
        assert api_client.post("/api/songs/123/download").status_code == 202
    assert download.call_args.kwargs == {"force_redownload": False}


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        True,
        {"force_redownload": "true"},
        {"force_redownload": 1},
        {"force": True},
    ],
)
def test_api_download_rejects_invalid_json(
    api_client: FlaskClient, body: object
) -> None:
    with patch.object(api.DownloadManager, "download") as download:
        response = api_client.post(
            "/api/songs/123/download",
            data=json.dumps(body),
            content_type="application/json",
        )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == 400
    download.assert_not_called()


@pytest.mark.parametrize(
    ("body", "content_type", "code"),
    [("{", "application/json", 400), ("force=true", "text/plain", 415)],
)
def test_api_download_rejects_malformed_body(
    api_client: FlaskClient, body: str, content_type: str, code: int
) -> None:
    response = api_client.post(
        "/api/songs/123/download", data=body, content_type=content_type
    )
    assert response.status_code == code
    assert response.is_json


@pytest.mark.parametrize(
    "status", [db.DownloadStatus.PENDING, db.DownloadStatus.DOWNLOADING]
)
def test_api_download_conflicts_with_active_job(
    api_client: FlaskClient, song: UsdbSong, status: db.DownloadStatus
) -> None:
    song.status = status
    with patch.object(api.DownloadManager, "download") as download:
        response = api_client.post(
            "/api/songs/123/download", json={"force_redownload": True}
        )
    assert response.status_code == 409
    download.assert_not_called()


def test_api_download_respects_pinned_song(
    api_client: FlaskClient, song: UsdbSong
) -> None:
    assert song.sync_meta
    song.sync_meta.pinned = True
    with patch.object(api.DownloadManager, "download") as download:
        response = api_client.post(
            "/api/songs/123/download", json={"force_redownload": True}
        )
    assert response.status_code == 409
    download.assert_not_called()


def test_api_read_only_server_allows_status_but_blocks_download(
    api_client: FlaskClient,
) -> None:
    app = webserver.create_app(
        "test", show_nonlocal_songs=True, allow_downloading=False
    )
    client = app.test_client()
    assert client.get("/api/songs/123/download").status_code == 200
    with patch.object(api.DownloadManager, "download") as download:
        response = client.post(
            "/api/songs/123/download", json={"force_redownload": True}
        )
    assert response.status_code == 403
    assert response.is_json
    download.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        "/api/missing",
        "/api/songs/123",
        "/api/songs/123/download",
        "/api/songs/123/audio",
    ],
)
def test_api_missing_song_and_routes_return_json(
    api_client: FlaskClient, path: str
) -> None:
    with patch.object(api.UsdbSong, "get", return_value=None):
        response = api_client.get(path)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == 404


def test_api_method_error_preserves_allow_header(api_client: FlaskClient) -> None:
    response = api_client.delete("/api/songs/123/download")
    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == 405
    assert "POST" in response.headers["Allow"]


@pytest.mark.parametrize(
    "query",
    [
        "limit=-1",
        "limit=0",
        "limit=1001",
        "limit=x",
        "offset=-1",
        "offset=x",
        "offset=999999999999999999999",
        "status=99",
        "status=abc",
        "year=x",
        "year=999999999999999999999",
        "rating=nan",
        "rating=x",
        "views=1",
        "views=5-1",
        "views=999999999999999999999-",
        "golden_notes=yes",
        "sort_by=bad",
        "sort_order=bad",
    ],
)
def test_api_rejects_invalid_search(api_client: FlaskClient, query: str) -> None:
    response = api_client.get(f"/api/songs?{query}")
    assert response.status_code == 400
    assert response.is_json


def test_browser_force_download(api_client: FlaskClient) -> None:
    with patch.object(api.DownloadManager, "download") as download:
        response = api_client.post(
            "/fragments/songs/123/download?force_redownload=true"
        )
    assert response.status_code == 200
    assert download.call_args.kwargs == {"force_redownload": True}
    assert "Force re-download" in response.text


def test_browser_local_download_polls(api_client: FlaskClient, song: UsdbSong) -> None:
    song.status = db.DownloadStatus.DOWNLOADING
    response = api_client.get("/fragments/songs/123/download")
    assert 'hx-trigger="every 3s"' in response.text
    assert 'title="Force re-download"' not in response.text


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "localhost"])  # noqa: S104
def test_cli_accepts_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    from usdb_syncer.gui import CliArgs

    monkeypatch.setattr(sys, "argv", ["usdb_syncer", "serve", "--host", host])
    assert CliArgs.parse().host == host


def test_server_binds_and_releases_port(monkeypatch: pytest.MonkeyPatch) -> None:
    app = flask.Flask(__name__)
    app.add_url_rule("/ping", view_func=lambda: "pong")
    monkeypatch.setattr(webserver, "create_app", lambda *_a, **_kw: app)
    manager = webserver._WebserverManager
    try:
        assert manager.start(host="127.0.0.1", port=0)
        assert not manager.start()
        port = manager.port
        response = requests.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert response.text == "pong"
        assert manager.stop()
        assert manager.start(host="127.0.0.1", port=port)
    finally:
        manager.stop()
    assert not manager.is_running()


def test_server_rejects_busy_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(errors.PortInUseError):
            webserver._WebserverManager.start(
                host="127.0.0.1", port=listener.getsockname()[1]
            )
    assert not webserver.is_running()


@pytest.mark.parametrize("port", [-1, 65536])
def test_server_rejects_invalid_port(port: int) -> None:
    with pytest.raises(errors.InvalidPortError):
        webserver._WebserverManager.start(host="127.0.0.1", port=port)


@pytest.mark.parametrize(
    ("host", "expected"),
    [("::1", "[::1]"), ("::", "[::1]"), ("0.0.0.0", "192.0.2.10")],  # noqa: S104
)
def test_address_is_usable(
    monkeypatch: pytest.MonkeyPatch, host: str, expected: str
) -> None:
    monkeypatch.setattr(webserver._WebserverManager, "host", host)
    monkeypatch.setattr(webserver._WebserverManager, "port", 8080)
    monkeypatch.setattr(webserver, "get_local_ip", lambda: "192.0.2.10")
    assert webserver.address() == f"http://{expected}:8080"


def test_api_audio_serves_existing_file(
    api_client: FlaskClient, song: UsdbSong, tmp_path: Path
) -> None:
    audio_path = tmp_path / "song.mp3"
    audio_path.write_bytes(b"example audio")
    with patch.object(api.UsdbSong, "audio_path", return_value=audio_path):
        response = api_client.get("/api/songs/123/audio")
    assert response.status_code == 200
    assert response.data == b"example audio"
    response.close()


def test_headless_processes_events_and_stops_downloads() -> None:
    from usdb_syncer import gui

    with (
        patch.object(gui.webserver, "start") as start,
        patch.object(gui.webserver, "stop") as stop,
        patch.object(gui.QtCore.QCoreApplication, "processEvents") as process,
        patch.object(gui.time, "sleep", side_effect=KeyboardInterrupt),
        patch.object(api.DownloadManager, "quit") as quit_downloads,
        patch.object(gui.db, "connect"),
        patch.object(gui.db, "close"),
    ):
        gui._run_webserver(host="127.0.0.1", port=8080, allow_downloading=True)
    assert start.call_args.kwargs["host"] == "127.0.0.1"
    process.assert_called_once()
    stop.assert_called_once()
    quit_downloads.assert_called_once()


def test_server_reports_binding_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_to_bind(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(webserver.werkzeug.serving, "make_server", fail_to_bind)
    with pytest.raises(errors.ServerBindError):
        webserver._WebserverManager.start(host="invalid-host", port=0)
    assert not webserver.is_running()


def test_api_search_with_real_database(
    monkeypatch: pytest.MonkeyPatch, song: UsdbSong, tmp_path: Path
) -> None:
    database = tmp_path / "api.sqlite"
    song.sync_meta = None
    monkeypatch.setattr(webserver.utils.AppPaths, "db", database)
    monkeypatch.setattr(
        webserver.db._DbState, "_local", webserver.db._LocalConnection()
    )
    api.UsdbSong.clear_cache()
    with db.managed_connection(database):
        song.upsert()
    api.UsdbSong.clear_cache()
    try:
        app = webserver.create_app(
            "test", show_nonlocal_songs=True, allow_downloading=False
        )
        client = app.test_client()
        result = client.get("/api/songs?search=Foo&limit=1&offset=0&status=0")
        assert result.status_code == 200
        assert result.get_json()[0]["song_id"] == int(song.song_id)
        assert client.get("/api/songs?search=absent").get_json() == []
        assert client.get("/api/songs?offset=1").get_json() == []
        local_app = webserver.create_app(
            "test", show_nonlocal_songs=False, allow_downloading=False
        )
        assert local_app.test_client().get("/api/songs").get_json() == []
    finally:
        api.UsdbSong.clear_cache()


@pytest.mark.parametrize("missing", [None, "txt", "audio"])
@pytest.mark.parametrize(
    "status", [db.DownloadStatus.SYNCHRONIZED, db.DownloadStatus.OUTDATED]
)
def test_api_downloaded_flag_requires_lyrics_and_audio(
    api_client: FlaskClient,
    song: UsdbSong,
    missing: str | None,
    status: db.DownloadStatus,
) -> None:
    song.status = status
    if missing:
        path = song.txt_path() if missing == "txt" else song.audio_path()
        assert path
        path.unlink()
    with patch.object(webserver, "_get_songs", return_value=[song]):
        for endpoint in ("/api/songs", "/api/songs/123", "/api/songs/123/download"):
            response = api_client.get(endpoint)
            assert response.status_code == 200
            payload = response.get_json()
            if isinstance(payload, list):
                payload = payload[0]
            assert payload["is_downloaded"] is (missing is None)
            if "is_local" in payload:
                assert payload["is_local"] is (missing is None)
            assert payload["status_code"] == int(status)


@pytest.mark.parametrize("missing", [None, "txt", "audio"])
def test_browser_only_offers_play_for_downloaded_songs(
    api_client: FlaskClient, song: UsdbSong, missing: str | None
) -> None:
    if missing:
        path = song.txt_path() if missing == "txt" else song.audio_path()
        assert path
        path.unlink()
    response = api_client.get("/fragments/songs/123/download")
    assert response.status_code == 200
    assert ('title="Play"' in response.text) is (missing is None)
    assert ('title="Force re-download"' in response.text) is (missing is None)
    assert ('title="Request download"' in response.text) is (missing is not None)


@pytest.mark.parametrize("show_nonlocal", [False, True])
def test_filter_downloaded_songs_before_pagination(
    app: flask.Flask, downloaded_song: UsdbSong, show_nonlocal: bool
) -> None:
    assert downloaded_song.sync_meta
    incomplete = attrs.evolve(downloaded_song.sync_meta, audio=None)
    songs = {
        SongId(i): attrs.evolve(
            downloaded_song,
            song_id=SongId(i),
            sync_meta=incomplete if i % 2 else downloaded_song.sync_meta,
        )
        for i in range(1, 5)
    }
    with (
        app.test_request_context("/?limit=1&offset=1"),
        patch.object(db, "search_usdb_songs", return_value=list(songs)),
        patch.object(api.UsdbSong, "get", side_effect=songs.get),
    ):
        selected = webserver._get_songs(flask.request, show_nonlocal, Counter())
    assert [song.song_id for song in selected] == [SongId(2 if show_nonlocal else 4)]
