"""Tests for UsdbSong."""

import json

import attrs
import pytest

from usdb_syncer.usdb_song import UsdbSong, UsdbSongEncoder


def test_encoding_and_decoding_song_meta(song: UsdbSong) -> None:
    song.sync_meta = None
    song_json = json.dumps(song, cls=UsdbSongEncoder)
    new_song = json.loads(song_json, object_hook=UsdbSong.from_json)
    assert isinstance(new_song, UsdbSong)
    assert attrs.asdict(song) == attrs.asdict(new_song)


@pytest.mark.parametrize(
    ("lyrics", "audio"), [(False, False), (False, True), (True, False), (True, True)]
)
def test_downloaded_requires_lyrics_and_audio(
    downloaded_song: UsdbSong, lyrics: bool, audio: bool
) -> None:
    for path, exists in (
        (downloaded_song.txt_path(), lyrics),
        (downloaded_song.audio_path(), audio),
    ):
        assert path
        if not exists:
            path.unlink()
    assert downloaded_song.is_downloaded() is (lyrics and audio)


@pytest.mark.parametrize("resource", ["txt", "audio"])
def test_downloaded_requires_resource_metadata(
    downloaded_song: UsdbSong, resource: str
) -> None:
    assert downloaded_song.sync_meta
    setattr(downloaded_song.sync_meta, resource, None)
    assert not downloaded_song.is_downloaded()


def test_downloaded_rechecks_files_on_disk(downloaded_song: UsdbSong) -> None:
    audio_path = downloaded_song.audio_path()
    assert audio_path
    assert downloaded_song.is_downloaded()
    audio_path.unlink()
    assert not downloaded_song.is_downloaded()
    audio_path.mkdir()
    assert not downloaded_song.is_downloaded()


def test_song_without_sync_meta_is_not_downloaded(song: UsdbSong) -> None:
    song.sync_meta = None
    assert not song.is_downloaded()
