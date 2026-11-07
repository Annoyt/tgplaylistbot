"""services/audiobook.py, services/recognizer.py, services/spotify.py."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from services import audiobook as ab
from services import recognizer as rec
from services import spotify as sp

_real_sleep = asyncio.sleep


def no_wait(*_):
    return _real_sleep(0)


# ── audiobook ──────────────────────────────────────────────────────────────

class TestAudiobookNormalize:
    @pytest.mark.parametrize("raw,expected", [
        ("Мастер и Маргарита (Аудиокнига) полная версия", "мастер и маргарита"),
        ("Fullmetal Alchemist audiobook", "fullmetal alchemist"),
        ("Книгами слушать онлайн", "книгами"),
    ])
    def test_stop_words_are_whole_words(self, raw, expected):
        assert ab._normalize(raw) == expected

    def test_rank_prefers_match_then_length(self, make_track):
        short = make_track(title="Мастер и Маргарита", artist="A", duration=3600)
        long_ = make_track(title="Мастер и Маргарита. Полная версия", artist="B", duration=50000)
        other = make_track(title="Война и мир", artist="C", duration=90000)
        out = ab._rank([other, short, long_], "Мастер и Маргарита")
        assert out[-1] is other and out[0] is long_


class TestAudiobookSearchAndDownload:
    async def test_provider_error_is_skipped(self, monkeypatch, make_track):
        good = make_track(title="Book")

        async def ok(q, c):
            return [good]

        async def boom(q, c):
            raise RuntimeError("down")
        monkeypatch.setattr(ab, "_SEARCH", {"a": ok, "b": boom})
        assert await ab.search("Book") == [good]

    async def test_unknown_source(self, make_track, tmp_settings):
        assert await ab.download(make_track(source="torrent")) is None

    async def test_yt_download_uses_youtube_flags_and_timeout(self, monkeypatch, tmp_path, make_track):
        seen = {}

        async def fake_run(*cmd, timeout):
            seen["cmd"], seen["timeout"] = cmd, timeout
            return 1, b"", b"ERROR: x"
        monkeypatch.setattr(ab, "yt_run", fake_run)
        assert await ab._yt_download(make_track(source_id="vid"), tmp_path) is None
        cmd = seen["cmd"]
        assert "youtube:player_client=default,android_vr" in cmd and "-4" in cmd and "!is_live" in cmd
        assert cmd[-1] == "https://www.youtube.com/watch?v=vid" and seen["timeout"] == ab._BOOK_DOWNLOAD_TIMEOUT_SEC

    async def test_chapter_fetch_streams_with_retries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab.asyncio, "sleep", no_wait)
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=b"chapter")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            assert await ab._fetch_chapter(c, "https://x/1.mp3", tmp_path / "1.mp3", 0)
        assert (tmp_path / "1.mp3").read_bytes() == b"chapter" and len(calls) == 3

    async def test_chapter_fetch_gives_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ab.asyncio, "sleep", no_wait)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))) as c:
            assert not await ab._fetch_chapter(c, "https://x/1.mp3", tmp_path / "1.mp3", 0)

    async def test_missing_chapter_fails_book_and_cleans_up(self, tmp_path, monkeypatch, make_track):
        monkeypatch.setattr(ab, "_librivox_sections", AsyncMock(return_value=["https://x/1.mp3", "https://x/2.mp3"]))

        async def fetch(client, url, dest, i):
            if i == 0:
                dest.write_bytes(b"ok")
                return True
            return False
        monkeypatch.setattr(ab, "_fetch_chapter", fetch)
        assert await ab._librivox_download(make_track(source="librivox", source_id="1"), tmp_path) is None
        assert list(tmp_path.iterdir()) == []         # parts dir removed


class TestPrepareParts:
    async def test_fits_whole(self, tmp_path, monkeypatch, tmp_settings):
        f = tmp_path / "book.mp3"
        f.write_bytes(b"x" * 1000)
        assert await ab.prepare_parts(str(f)) == [str(f)]

    async def test_split_segment_length_from_real_bitrate(self, tmp_path, monkeypatch, tmp_settings):
        f = tmp_path / "book.mp3"
        f.write_bytes(b"x")
        monkeypatch.setattr(tmp_settings, "telegram_api_base_url", "")          # 50 MB limit
        monkeypatch.setattr(ab.os.path, "getsize", lambda p: 100 * 1024 * 1024)  # 100 MB book
        monkeypatch.setattr(ab, "ffprobe_duration", AsyncMock(return_value=10000))
        seen = {}

        class Proc:
            returncode = 0

            async def communicate(self):
                for i in range(3):
                    (tmp_path / f"book_part_{i:03d}.mp3").write_bytes(b"p")
                return b"", b""

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = cmd
            return Proc()
        monkeypatch.setattr(ab.asyncio, "create_subprocess_exec", fake_exec)
        parts = await ab.prepare_parts(str(f))
        # 45 MB target * 10000 s / 100 MB = 4500 s per part
        assert seen["cmd"][seen["cmd"].index("-segment_time") + 1] == "4500"
        assert parts == [str(tmp_path / f"book_part_{i:03d}.mp3") for i in range(3)]

    async def test_failed_split_removes_partial_parts(self, tmp_path, monkeypatch, tmp_settings):
        f = tmp_path / "book.mp3"
        f.write_bytes(b"x")
        monkeypatch.setattr(tmp_settings, "telegram_api_base_url", "")
        monkeypatch.setattr(ab.os.path, "getsize", lambda p: 100 * 1024 * 1024)
        monkeypatch.setattr(ab, "ffprobe_duration", AsyncMock(return_value=0))

        class Proc:
            returncode = 1

            async def communicate(self):
                (tmp_path / "book_part_000.mp3").write_bytes(b"half")
                return b"", b"disk full"

        async def fake_exec(*cmd, **kw):
            return Proc()
        monkeypatch.setattr(ab.asyncio, "create_subprocess_exec", fake_exec)
        assert await ab.prepare_parts(str(f)) == [str(f)]
        assert not (tmp_path / "book_part_000.mp3").exists()


# ── recognizer ─────────────────────────────────────────────────────────────

class TestShazamAlbum:
    @pytest.mark.parametrize("track,album", [
        ({}, ""),
        ({"sections": []}, ""),
        ({"sections": [{"metadata": []}]}, ""),                       # used to raise IndexError
        ({"sections": [{"metadata": [{"text": "Evolve"}]}]}, "Evolve"),
        ({"sections": [{}, {"metadata": [{"title": "x"}, {"text": "Night Visions"}]}]}, "Night Visions"),
    ])
    def test_album(self, track, album):
        assert rec._shazam_album(track) == album


class TestRecognize:
    @pytest.fixture
    def wav(self, tmp_path, monkeypatch):
        w = tmp_path / "in.wav"

        async def convert(path):
            w.write_bytes(b"wav")
            return str(w)
        monkeypatch.setattr(rec, "_convert_to_wav", convert)
        return w

    async def test_shazam_hit(self, wav, monkeypatch):
        hit = rec.RecognizedTrack(title="T", artist="A")
        monkeypatch.setattr(rec, "recognize_shazam", AsyncMock(return_value=hit))
        assert await rec.recognize("x.mp3") is hit
        assert not wav.exists()

    async def test_speed_variants_tried_in_order_and_cleaned(self, wav, monkeypatch, tmp_path):
        tried = []

        async def variant(path, factor):
            v = tmp_path / f"v{factor}.wav"
            v.write_bytes(b"v")
            tried.append(factor)
            return str(v)
        hit = rec.RecognizedTrack(title="T", artist="A")
        monkeypatch.setattr(rec, "_speed_variant", variant)
        monkeypatch.setattr(rec, "recognize_shazam", AsyncMock(side_effect=[None, None, hit]))
        assert await rec.recognize("x.mp3") is hit
        assert tried == [0.8, 1.25]
        assert list(tmp_path.glob("v*.wav")) == [] and not wav.exists()

    async def test_acoustid_only_with_key(self, wav, monkeypatch):
        monkeypatch.setattr(rec, "recognize_shazam", AsyncMock(return_value=None))
        ac = AsyncMock(return_value=None)
        monkeypatch.setattr(rec, "recognize_acoustid", ac)
        await rec.recognize("x.mp3", "", try_speed_variants=False)
        ac.assert_not_called()
        await rec.recognize("x.mp3", "key", try_speed_variants=False)
        ac.assert_awaited_once()

    async def test_acoustid_runs_off_the_event_loop(self, monkeypatch):
        import threading
        main = threading.get_ident()
        seen = {}

        def fake_match(key, path, timeout):
            seen["thread"], seen["timeout"] = threading.get_ident(), timeout
            return [(0.9, "rid", "Title", "Artist")]
        monkeypatch.setitem(__import__("sys").modules, "acoustid", SimpleNamespace(match=fake_match))
        res = await rec.recognize_acoustid("x.wav", "key")
        assert (res.title, res.artist) == ("Title", "Artist")
        assert seen["thread"] != main and seen["timeout"] == 15

    async def test_acoustid_low_score_ignored(self, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "acoustid",
                            SimpleNamespace(match=lambda k, p, timeout: [(0.3, "r", "T", "A")]))
        assert await rec.recognize_acoustid("x.wav", "key") is None

    async def test_wav_conversion_is_capped_and_skips_video(self, monkeypatch):
        seen = {}

        class Proc:
            async def wait(self):
                return 0

        async def fake_exec(*cmd, **kw):
            seen["cmd"] = cmd
            return Proc()
        monkeypatch.setattr(rec.asyncio, "create_subprocess_exec", fake_exec)
        out = await rec._convert_to_wav("in.mp4")
        cmd = seen["cmd"]
        assert cmd[cmd.index("-t") + 1] == str(rec._WAV_MAX_SEC) and "-vn" in cmd and cmd[-1] == out
        import os
        os.remove(out)


# ── spotify ────────────────────────────────────────────────────────────────

class TestSpotify:
    def test_no_credentials_no_client(self, monkeypatch):
        monkeypatch.setattr(sp.settings, "spotify_client_id", "")
        assert sp._get_sp() is None

    def test_client_has_no_retries_and_a_timeout(self, monkeypatch):
        monkeypatch.setattr(sp.settings, "spotify_client_id", "id")
        monkeypatch.setattr(sp.settings, "spotify_client_secret", "secret")
        client = sp._get_sp()
        assert client.requests_timeout == 5 and client.retries == 0 and client.status_retries == 0

    async def test_search_maps_items(self, monkeypatch):
        item = {"name": "Believer", "duration_ms": 204_500, "id": "sp1",
                "artists": [{"name": "Imagine Dragons"}, {"name": "X"}],
                "album": {"name": "Evolve", "images": [{"url": "http://img"}]}}
        fake = SimpleNamespace(search=lambda **kw: {"tracks": {"items": [item]}})
        monkeypatch.setattr(sp, "_get_sp", lambda: fake)
        (t,) = await sp.search("believer")
        assert (t.title, t.artist, t.duration, t.source, t.source_id, t.album, t.thumbnail_url) == \
            ("Believer", "Imagine Dragons, X", 204, "spotify", "sp1", "Evolve", "http://img")

    async def test_search_error_returns_empty(self, monkeypatch):
        def boom(**kw):
            raise RuntimeError("429")
        monkeypatch.setattr(sp, "_get_sp", lambda: SimpleNamespace(search=boom))
        assert await sp.search("x") == []
