"""services/vk_music.py with a fake vk_api session (no network)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers.admin import update_global_setting
from services import vk_music as vk


class FakeVk:
    """Stands in for VkAudio: records API calls, returns canned responses."""

    def __init__(self, responses):
        self.calls = []
        self._responses = responses
        self._vk = self

    def method(self, name, params):
        self.calls.append((name, params))
        r = self._responses[name]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def fake_vk(monkeypatch):
    holder = SimpleNamespace(api=None)

    def init(creds, captcha_handler=None):
        holder.creds = creds
        return holder.api
    monkeypatch.setattr(vk, "_init_vk_audio", init)
    monkeypatch.setattr(vk.settings, "vk_token", "env-token")
    return holder


ITEMS = [
    {"title": "Believer", "artist": "Imagine Dragons", "duration": 204, "owner_id": -1, "id": 5, "is_hq": True},
    {"title": "Old", "artist": "X", "duration": "180", "owner_id": 7, "id": 9},
]


class TestCreds:
    async def test_db_overrides_env_and_blank_values_are_ignored(self, db, monkeypatch):
        monkeypatch.setattr(vk.settings, "vk_token", "env-token")
        monkeypatch.setattr(vk.settings, "vk_login", "env-login")
        await update_global_setting("vk_token", "db-token")
        await update_global_setting("vk_login", "")
        creds = await vk._get_vk_creds()
        assert creds["token"] == "db-token" and creds["login"] == "env-login"

    async def test_db_failure_falls_back_to_env(self, tmp_settings, monkeypatch):
        monkeypatch.setattr(vk.settings, "vk_token", "env-token")

        async def broken():
            raise RuntimeError("db down")
        monkeypatch.setattr("app.db.database.get_db", broken)
        assert (await vk._get_vk_creds())["token"] == "env-token"


class TestSearch:
    async def test_maps_items_and_hq_bitrate(self, db, fake_vk):
        fake_vk.api = FakeVk({"audio.search": {"items": ITEMS}})
        tracks = await vk.search("believer", count=5)
        assert fake_vk.api.calls == [("audio.search", {"q": "believer", "count": 5, "sort": 2, "autocomplete": 1})]
        a, b = tracks
        assert (a.title, a.artist, a.duration, a.source, a.source_id, a.bitrate) == \
            ("Believer", "Imagine Dragons", 204, "vk", "https://vk.com/audio-1_5", 320)
        assert (b.duration, b.bitrate, b.source_id) == (180, 128, "https://vk.com/audio7_9")

    async def test_no_credentials(self, db, monkeypatch):
        monkeypatch.setattr(vk.settings, "vk_token", "")
        monkeypatch.setattr(vk.settings, "vk_login", "")
        assert await vk.search("x") == []

    async def test_init_failure(self, db, fake_vk):
        fake_vk.api = None
        assert await vk.search("x") == []

    async def test_api_error(self, db, fake_vk):
        fake_vk.api = FakeVk({"audio.search": RuntimeError("captcha")})
        assert await vk.search("x") == []


class TestAddAndPlaylist:
    async def test_add_parses_owner_and_id(self, db, fake_vk, make_track):
        fake_vk.api = FakeVk({"audio.add": 1})
        assert await vk.add_track_to_my_audios(make_track(source="vk", source_id="https://vk.com/audio-12_34"))
        assert fake_vk.api.calls == [("audio.add", {"audio_id": "34", "owner_id": "-12"})]

    @pytest.mark.parametrize("source_id", ["", "https://youtube.com/x", "https://vk.com/audio_bad"])
    async def test_add_skips_unparseable(self, db, fake_vk, make_track, source_id):
        fake_vk.api = FakeVk({})
        assert not await vk.add_track_to_my_audios(make_track(source_id=source_id))

    async def test_playlist_params(self, db, fake_vk):
        fake_vk.api = FakeVk({"audio.get": {"items": ITEMS[:1]}})
        tracks = await vk.get_playlist_tracks("-147", "2949", access_key="abc")
        assert fake_vk.api.calls == [("audio.get", {"owner_id": -147, "count": 200, "album_id": 2949, "access_key": "abc"})]
        assert [t.title for t in tracks] == ["Believer"]

    async def test_playlist_zero_album_means_all_audios(self, db, fake_vk):
        fake_vk.api = FakeVk({"audio.get": {"items": []}})
        await vk.get_playlist_tracks("5", "0")
        assert fake_vk.api.calls == [("audio.get", {"owner_id": 5, "count": 200})]


class TestDownload:
    @pytest.fixture
    def hls(self, monkeypatch):
        import services.hls_downloader as h
        state = SimpleNamespace(kind="hls", hls_ok=True, size=1000, fallback=None)

        async def detect(url, headers=None):
            state.detected_url = url
            return state.kind

        async def download_hls(url, output_path, headers=None, expected_duration=0):
            if state.hls_ok:
                output_path.write_bytes(b"x" * state.size)
            return state.hls_ok

        async def direct(url, output_path, headers=None, max_size_bytes=0):
            output_path.write_bytes(b"d")
            return True
        monkeypatch.setattr(h, "detect_url_type", detect)
        monkeypatch.setattr(h, "download_hls", download_hls)
        monkeypatch.setattr(h, "download_direct_streaming", direct)
        fb = AsyncMock(return_value="/fallback.mp3")
        monkeypatch.setattr(vk, "_ytdlp_fallback", fb)
        state.fallback = fb
        return state

    async def test_resolves_url_then_hls(self, db, fake_vk, hls, make_track, tmp_path):
        fake_vk.api = FakeVk({"audio.getById": [{"url": "https://cdn/x/index.m3u8"}]})
        path = await vk.download(make_track(source="vk", source_id="https://vk.com/audio-1_5"), download_dir=tmp_path)
        assert fake_vk.api.calls == [("audio.getById", {"audios": "-1_5"})]
        assert hls.detected_url == "https://cdn/x/index.m3u8"
        assert path.endswith(".mp3") and (tmp_path / path.rsplit("/", 1)[1]).exists()

    async def test_hls_failure_falls_back_to_ytdlp(self, db, fake_vk, hls, make_track, tmp_path):
        fake_vk.api = FakeVk({"audio.getById": [{"url": "https://cdn/x/index.m3u8"}]})
        hls.hls_ok = False
        assert await vk.download(make_track(source="vk", source_id="https://vk.com/audio-1_5"), download_dir=tmp_path) == "/fallback.mp3"

    async def test_oversized_hls_result_is_dropped(self, db, fake_vk, hls, make_track, tmp_path, monkeypatch):
        fake_vk.api = FakeVk({"audio.getById": [{"url": "https://cdn/x/index.m3u8"}]})
        monkeypatch.setattr(vk.settings, "max_file_size_mb", 0)
        assert await vk.download(make_track(source="vk", source_id="https://vk.com/audio-1_5"), download_dir=tmp_path) is None
        assert list(tmp_path.glob("*.mp3")) == []      # oversized file removed

    async def test_direct_url(self, db, fake_vk, hls, make_track, tmp_path):
        fake_vk.api = FakeVk({"audio.getById": [{"url": "https://cdn/x/track.mp3?extra=1"}]})
        hls.kind = "direct"
        path = await vk.download(make_track(source="vk", source_id="https://vk.com/audio-1_5"), download_dir=tmp_path)
        assert path and path.endswith(".mp3")

    async def test_unresolvable_url(self, db, fake_vk, hls, make_track, tmp_path):
        fake_vk.api = FakeVk({"audio.getById": []})
        assert await vk.download(make_track(source="vk", source_id="https://vk.com/audio-1_5"), download_dir=tmp_path) is None


class TestFallback:
    async def test_uses_timeout_live_filter_and_readable_errors(self, monkeypatch, tmp_path, caplog):
        seen = {}

        async def fake_run(*cmd, timeout):
            seen["cmd"], seen["timeout"] = cmd, timeout
            return 1, b"", b"[generic] x\nERROR: Unable to download\n"
        monkeypatch.setattr(vk, "_run", fake_run)
        assert await vk._ytdlp_fallback("https://cdn/x.m3u8", tmp_path, "uid") is None
        assert seen["timeout"] == vk.AUDIO_TIMEOUT_SEC and "!is_live" in seen["cmd"]
        assert seen["cmd"][-1] == "https://cdn/x.m3u8"
        assert "ERROR: Unable to download" in caplog.text
