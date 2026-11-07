"""services/cache.py and services/downloader.py."""

import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import cache, downloader


class TestNormalizeName:
    @pytest.mark.parametrize("raw,expected", [
        ("Believer (Official Video)", "believer"),
        ("Song [HD] feat. Someone Else", "song"),
        ("Song ft. X", "song"),
        ("  Мама   Люба!!! ", "мама люба"),
        ("Ёлка", "ёлка"),
        ("밤편지", "밤편지"),
        ("Beyoncé", "beyoncé"),
        ("", ""),
    ])
    def test_normalize(self, raw, expected):
        assert cache._normalize_name(raw) == expected


class TestTrackHash:
    def test_format(self):
        assert cache.generate_track_hash("Imagine Dragons", "Believer (Lyrics)", 204) == "imagine dragons_believer_204"

    def test_duration_distinguishes_versions(self):
        assert cache.generate_track_hash("A", "B", 200) != cache.generate_track_hash("A", "B", 260)

    def test_non_latin_songs_no_longer_collide(self):
        a = cache.generate_track_hash("아이유", "밤편지", 253)
        b = cache.generate_track_hash("아이유", "좋은 날", 253)
        assert a and b and a != b

    @pytest.mark.parametrize("artist,title", [("", "Song"), ("Artist", ""), ("!!!", "Song"), ("Artist", "(Live)")])
    def test_empty_name_is_uncacheable(self, artist, title):
        assert cache.generate_track_hash(artist, title, 100) == ""


class TestCacheDb:
    async def test_round_trip(self, db):
        assert await cache.get_cached_file_id("A", "B", 100) is None
        assert await cache.save_cached_file_id("A", "B", 100, "file-1")
        assert await cache.get_cached_file_id("A", "B (Official)", 100) == "file-1"

    async def test_newer_file_id_replaces_stale_one(self, db):
        await cache.save_cached_file_id("A", "B", 100, "stale")
        await cache.save_cached_file_id("A", "B", 100, "fresh")
        assert await cache.get_cached_file_id("A", "B", 100) == "fresh"

    async def test_uncacheable_names_skip_db(self, db):
        assert await cache.save_cached_file_id("", "", 100, "x") is False
        assert await cache.get_cached_file_id("", "", 100) is None

    async def test_db_error_is_swallowed(self, tmp_settings, monkeypatch):
        async def broken():
            raise RuntimeError("db down")
        monkeypatch.setattr("app.db.database.get_db", broken)
        assert await cache.get_cached_file_id("A", "B", 1) is None
        assert await cache.save_cached_file_id("A", "B", 1, "x") is False


class TestCleanup:
    def test_cleanup_file(self, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        downloader.cleanup_file(str(f))
        assert not f.exists()
        downloader.cleanup_file(str(f))     # missing file is fine

    def test_cleanup_dir_only_old_files(self, tmp_settings):
        d = tmp_settings.download_path
        old, new = d / "old.mp3", d / "new.mp3"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        (d / "sub").mkdir()
        past = time.time() - 7200
        os.utime(old, (past, past))
        assert downloader.cleanup_download_dir(older_than_sec=3600) == 1
        assert not old.exists() and new.exists() and (d / "sub").exists()

    def test_cleanup_dir_boundary(self, tmp_settings):
        f = tmp_settings.download_path / "edge.mp3"
        f.write_bytes(b"x")
        t = time.time() - 100
        os.utime(f, (t, t))
        assert downloader.cleanup_download_dir(older_than_sec=200) == 0
        assert downloader.cleanup_download_dir(older_than_sec=50) == 1


class FakeProc:
    def __init__(self, rc=0, hook=None):
        self.returncode, self._hook = rc, hook

    async def communicate(self):
        if self._hook:
            self._hook()
        return b"", b""


@pytest.fixture
def no_delay(monkeypatch):
    async def settings_():
        return SimpleNamespace(download_delay_sec=0)
    monkeypatch.setattr(downloader, "get_global_settings", settings_)


class TestDownloadTrack:
    async def test_youtube_route_and_id3_tagging(self, monkeypatch, tmp_path, make_track, no_delay):
        src = tmp_path / "x.mp3"
        src.write_bytes(b"audio")
        monkeypatch.setattr(downloader.yt_service, "download", AsyncMock(return_value=str(src)))
        seen = []

        async def fake_exec(*cmd, **kw):
            seen.append(cmd)
            return FakeProc(hook=lambda: open(cmd[-1], "wb").write(b"tagged"))
        monkeypatch.setattr(downloader.asyncio, "create_subprocess_exec", fake_exec)
        path = await downloader.download_track(make_track(title='Say "Hi"', artist="A"), "mp3_320")
        assert path == str(src) and src.read_bytes() == b"tagged"
        cmd = seen[0]
        assert cmd[-1] == str(tmp_path / "x.tagged.mp3")
        assert 'title=Say "Hi"' in cmd and "artist=A" in cmd

    async def test_flac_tag_output_keeps_extension_last(self, monkeypatch, tmp_path, make_track, no_delay):
        src = tmp_path / "x.flac"
        src.write_bytes(b"audio")
        monkeypatch.setattr(downloader.yt_service, "download", AsyncMock(return_value=str(src)))
        seen = []

        async def fake_exec(*cmd, **kw):
            seen.append(cmd)
            return FakeProc(rc=1)
        monkeypatch.setattr(downloader.asyncio, "create_subprocess_exec", fake_exec)
        assert await downloader.download_track(make_track(), "flac") == str(src)
        assert seen[0][-1].endswith(".tagged.flac")
        assert src.read_bytes() == b"audio"            # failed tagging leaves the original

    async def test_spotify_downloads_via_youtube_search(self, monkeypatch, tmp_path, make_track, no_delay):
        yt_track = make_track(source="youtube", source_id="yt1")
        search = AsyncMock(return_value=[yt_track])
        dl = AsyncMock(return_value=None)
        monkeypatch.setattr(downloader.yt_service, "search", search)
        monkeypatch.setattr(downloader.yt_service, "download", dl)
        assert await downloader.download_track(make_track(source="spotify", artist="A", title="B")) is None
        search.assert_awaited_once_with("A B", count=1)
        dl.assert_awaited_once_with(yt_track, "mp3_320")

    async def test_vk_route_adds_to_my_audios_first(self, monkeypatch, make_track, no_delay):
        order = []
        monkeypatch.setattr(downloader.vk_service, "add_track_to_my_audios",
                            AsyncMock(side_effect=lambda *a, **k: order.append("add")))
        monkeypatch.setattr(downloader.vk_service, "download",
                            AsyncMock(side_effect=lambda *a, **k: order.append("dl")))
        assert await downloader.download_track(make_track(source="vk")) is None
        assert order == ["add", "dl"]

    async def test_unknown_source(self, make_track, no_delay):
        assert await downloader.download_track(make_track(source="deezer")) is None

    async def test_delay_between_downloads(self, monkeypatch, make_track):
        async def settings_():
            return SimpleNamespace(download_delay_sec=5)
        monkeypatch.setattr(downloader, "get_global_settings", settings_)
        slept = []

        async def fake_sleep(sec):
            slept.append(sec)
        monkeypatch.setattr(downloader.asyncio, "sleep", fake_sleep)
        clock = [1000.0]
        monkeypatch.setattr(downloader.time, "monotonic", lambda: clock[0])
        monkeypatch.setattr(downloader, "_last_download_time", 998.0)
        await downloader.download_track(make_track(source="deezer"))
        assert slept == [pytest.approx(3.0)]
