"""services/hls_downloader.py: playlist parsing, IVs, AES-128 decryption, gap handling."""

import asyncio
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from services import hls_downloader as hls

BASE = "https://cdn.example/audio/"
_real_sleep = asyncio.sleep


def no_wait(*_):
    return _real_sleep(0)

KEY = bytes(range(16))


def encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


class TestUrls:
    def test_base_url_drops_query_and_file(self):
        assert hls._get_base_url("https://a.b/x/y/index.m3u8?sig=1") == "https://a.b/x/y/"

    def test_resolve_relative_and_absolute(self):
        assert hls._resolve_url("seg1.ts", BASE) == BASE + "seg1.ts"
        assert hls._resolve_url("../k/key.bin", BASE) == "https://cdn.example/k/key.bin"
        assert hls._resolve_url("https://other/seg.ts", BASE) == "https://other/seg.ts"


class TestParseIv:
    @pytest.mark.parametrize("hexs,expected", [
        ("00000000000000000000000000000001", (1).to_bytes(16, "big")),
        ("1", (1).to_bytes(16, "big")),            # short / odd length
        ("FF" * 16, b"\xff" * 16),
    ])
    def test_valid(self, hexs, expected):
        assert hls._parse_iv(hexs) == expected

    def test_too_long_is_none(self):
        assert hls._parse_iv("1" + "00" * 16) is None


class TestParseMediaPlaylist:
    def test_plain_segments_with_durations(self):
        segs = hls._parse_media_playlist("#EXTM3U\n#EXTINF:10.0,\na.ts\n#EXTINF:4.5,\nb.ts\n#EXT-X-ENDLIST", BASE)
        assert [(s.url, s.duration, s.index, s.key_method) for s in segs] == [
            (BASE + "a.ts", 10.0, 0, "NONE"), (BASE + "b.ts", 4.5, 1, "NONE")]

    def test_key_switches_and_resets(self):
        m3u8 = "\n".join([
            "#EXTM3U",
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x000000000000000000000000000000AA',
            "#EXTINF:10,", "a.ts",
            "#EXT-X-KEY:METHOD=NONE",
            "#EXTINF:10,", "b.ts",
            '#EXT-X-KEY:METHOD=AES-128,URI="https://k/2"',
            "#EXTINF:10,", "c.ts",
        ])
        a, b, c = hls._parse_media_playlist(m3u8, BASE)
        assert (a.key_method, a.key_url, a.iv) == ("AES-128", BASE + "key.bin", (0xAA).to_bytes(16, "big"))
        assert (b.key_method, b.key_url, b.iv) == ("NONE", None, None)
        assert (c.key_method, c.key_url, c.iv) == ("AES-128", "https://k/2", (2).to_bytes(16, "big"))

    def test_uppercase_hex_prefix_iv(self):
        segs = hls._parse_media_playlist('#EXT-X-KEY:METHOD=AES-128,URI="k",IV=0X0F\n#EXTINF:1,\na.ts', BASE)
        assert segs[0].iv == (0x0F).to_bytes(16, "big")

    def test_implicit_iv_uses_media_sequence(self):
        m3u8 = '#EXT-X-MEDIA-SEQUENCE:7\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts'
        a, b = hls._parse_media_playlist(m3u8, BASE)
        assert a.iv == (7).to_bytes(16, "big") and b.iv == (8).to_bytes(16, "big")
        assert (a.index, b.index) == (0, 1)

    def test_skips_comment_lines_between_extinf_and_uri(self):
        segs = hls._parse_media_playlist("#EXTINF:3,\n#EXT-X-BYTERANGE:10@0\nseg.ts", BASE)
        assert [s.url for s in segs] == [BASE + "seg.ts"]


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestResolveSegments:
    async def test_master_picks_highest_bandwidth_variant(self):
        def handler(req):
            if req.url.path.endswith("master.m3u8"):
                return httpx.Response(200, text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nlow.m3u8\n"
                                                "#EXT-X-STREAM-INF:BANDWIDTH=320000\nhigh.m3u8\n")
            assert req.url.path.endswith("high.m3u8")
            return httpx.Response(200, text="#EXTINF:5,\nh1.ts")
        async with client_for(handler) as c:
            segs = await hls._resolve_segments(c, BASE + "master.m3u8")
        assert [s.url for s in segs] == [BASE + "h1.ts"]

    async def test_master_without_variant_is_empty(self):
        async with client_for(lambda r: httpx.Response(200, text="#EXT-X-STREAM-INF:BANDWIDTH=1\n")) as c:
            assert await hls._resolve_segments(c, BASE + "m.m3u8") == []


class TestKeyAndSegments:
    async def test_fetch_key_requires_16_bytes(self, monkeypatch):
        monkeypatch.setattr(hls.asyncio, "sleep", no_wait)
        async with client_for(lambda r: httpx.Response(200, content=KEY)) as c:
            assert await hls._fetch_key(c, "https://k") == KEY
        async with client_for(lambda r: httpx.Response(200, content=b"short")) as c:
            assert await hls._fetch_key(c, "https://k") is None

    async def test_decrypts_and_unpads(self, tmp_path):
        plain = b"\x47" + b"TS-PAYLOAD" * 50
        iv = (7).to_bytes(16, "big")
        seg = hls.HLSSegment(BASE + "a.ts", 1.0, 0, key_url="k", key_method="AES-128", iv=iv)
        async with client_for(lambda r: httpx.Response(200, content=encrypt(plain, KEY, iv))) as c:
            ok = await hls._download_and_decrypt_segment(c, seg, tmp_path / "a.ts", {"k": KEY}, asyncio.Semaphore(1))
        assert ok and (tmp_path / "a.ts").read_bytes() == plain

    async def test_plain_segment_written_as_is(self, tmp_path):
        seg = hls.HLSSegment(BASE + "a.ts", 1.0, 0)
        async with client_for(lambda r: httpx.Response(200, content=b"raw")) as c:
            assert await hls._download_and_decrypt_segment(c, seg, tmp_path / "a.ts", {}, asyncio.Semaphore(1))
        assert (tmp_path / "a.ts").read_bytes() == b"raw"

    async def test_missing_key_fails(self, tmp_path):
        seg = hls.HLSSegment(BASE + "a.ts", 1.0, 0, key_url="k", key_method="AES-128", iv=b"\0" * 16)
        async with client_for(lambda r: httpx.Response(200, content=b"x" * 16)) as c:
            assert not await hls._download_and_decrypt_segment(c, seg, tmp_path / "a.ts", {}, asyncio.Semaphore(1))

    async def test_retries_then_gives_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hls.asyncio, "sleep", no_wait)
        hits = []

        def handler(r):
            hits.append(1)
            return httpx.Response(503)
        seg = hls.HLSSegment(BASE + "a.ts", 1.0, 0)
        async with client_for(handler) as c:
            assert not await hls._download_and_decrypt_segment(c, seg, tmp_path / "a.ts", {}, asyncio.Semaphore(1))
        assert len(hits) == hls._MAX_RETRIES


class TestDownloadHls:
    @pytest.fixture
    def mocked(self, monkeypatch, tmp_path):
        """download_hls against a fake CDN; ffmpeg concat + ffprobe stubbed."""
        state = {"fail": set()}
        playlist = "#EXTM3U\n" + "".join(f"#EXTINF:10,\ns{i}.ts\n" for i in range(3))

        def handler(req):
            name = req.url.path.rsplit("/", 1)[-1]
            if name == "index.m3u8":
                return httpx.Response(200, text=playlist)
            if name in state["fail"]:
                return httpx.Response(403)
            return httpx.Response(200, content=name.encode())

        real = httpx.AsyncClient
        monkeypatch.setattr(hls.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        monkeypatch.setattr(hls.asyncio, "sleep", no_wait)

        async def fake_concat(seg_files, out):
            state["concat"] = [f.read_bytes() for f in seg_files]
            Path(out).write_bytes(b"mp3")
            return True

        async def fake_probe(path):
            return 30.0
        monkeypatch.setattr(hls, "_concat_with_ffmpeg", fake_concat)
        monkeypatch.setattr(hls, "get_duration_ffprobe", fake_probe)
        return state

    async def test_all_segments_in_order(self, mocked, tmp_path):
        out = tmp_path / "out.mp3"
        assert await hls.download_hls(BASE + "index.m3u8", out, expected_duration=30)
        assert mocked["concat"] == [b"s0.ts", b"s1.ts", b"s2.ts"]

    async def test_any_failed_segment_fails_the_download(self, mocked, tmp_path):
        mocked["fail"] = {"s1.ts"}
        assert not await hls.download_hls(BASE + "index.m3u8", tmp_path / "out.mp3")
        assert "concat" not in mocked


# ── written to kill surviving mutants (mutmut) ─────────────────────────────

async def resolve_master(master_text, variants):
    def handler(req):
        name = req.url.path.rsplit("/", 1)[-1]
        if name == "master.m3u8":
            return httpx.Response(200, text=master_text)
        return httpx.Response(200, text=f"#EXTINF:1,\n{variants[name]}")
    async with client_for(handler) as c:
        return [s.url for s in await hls._resolve_segments(c, BASE + "master.m3u8")]


class TestMasterPlaylistEdges:
    async def test_best_variant_listed_first(self):
        master = "#EXT-X-STREAM-INF:BANDWIDTH=320000\nhigh.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=64000\nlow.m3u8\n"
        assert await resolve_master(master, {"high.m3u8": "h.ts", "low.m3u8": "l.ts"}) == [BASE + "h.ts"]

    async def test_variant_without_bandwidth_is_still_usable(self):
        master = "#EXT-X-STREAM-INF:CODECS=\"mp4a\"\nonly.m3u8\n"
        assert await resolve_master(master, {"only.m3u8": "o.ts"}) == [BASE + "o.ts"]

    async def test_bandwidth_beats_missing_bandwidth(self):
        master = "#EXT-X-STREAM-INF:CODECS=\"x\"\nnobw.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1\nbw.m3u8\n"
        assert await resolve_master(master, {"nobw.m3u8": "n.ts", "bw.m3u8": "b.ts"}) == [BASE + "b.ts"]

    async def test_equal_bandwidth_keeps_first(self):
        master = "#EXT-X-STREAM-INF:BANDWIDTH=5\na.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=5\nb.m3u8\n"
        assert await resolve_master(master, {"a.m3u8": "a.ts", "b.m3u8": "b.ts"}) == [BASE + "a.ts"]

    async def test_blank_lines_before_uri_are_skipped(self):
        master = "#EXT-X-STREAM-INF:BANDWIDTH=5\n\n   \nv.m3u8\n"
        assert await resolve_master(master, {"v.m3u8": "v.ts"}) == [BASE + "v.ts"]


class TestMediaPlaylistEdges:
    def test_plain_segments_have_no_key_or_iv(self):
        (seg,) = hls._parse_media_playlist("#EXTINF:2,\na.ts", BASE)
        assert seg.key_url is None and seg.iv is None and seg.key_method == "NONE"

    def test_key_without_method_counts_as_none(self):
        (seg,) = hls._parse_media_playlist('#EXT-X-KEY:URI="k",IV=0x1\n#EXTINF:2,\na.ts', BASE)
        assert (seg.key_method, seg.key_url, seg.iv) == ("NONE", None, None)

    def test_method_none_clears_previous_key_even_with_uri(self):
        m3u8 = '#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXT-X-KEY:METHOD=NONE,URI="x",IV=0x2\n#EXTINF:2,\na.ts'
        (seg,) = hls._parse_media_playlist(m3u8, BASE)
        assert (seg.key_url, seg.iv) == (None, None)

    def test_extinf_without_number_is_zero_duration(self):
        (seg,) = hls._parse_media_playlist("#EXTINF:,title\na.ts", BASE)
        assert seg.duration == 0.0


class TestEncryptedDownload:
    async def test_aes_segments_key_fetched_once_headers_and_redirects(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hls.asyncio, "sleep", no_wait)
        plains = [b"\x47" + bytes([i]) * 300 for i in range(3)]
        key_hits, seen_referers = [], set()
        m3u8 = ('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
                + "".join(f"#EXTINF:10,\ns{i}.ts\n" for i in range(3)))

        def handler(req):
            seen_referers.add(req.headers.get("referer"))
            name = req.url.path.rsplit("/", 1)[-1]
            if name == "start.m3u8":
                return httpx.Response(302, headers={"location": BASE + "index.m3u8"})
            if name == "index.m3u8":
                return httpx.Response(200, text=m3u8)
            if name == "key.bin":
                key_hits.append(1)
                return httpx.Response(200, content=KEY)
            i = int(name[1])
            return httpx.Response(200, content=encrypt(plains[i], KEY, (5 + i).to_bytes(16, "big")))

        real = httpx.AsyncClient
        monkeypatch.setattr(hls.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        got = {}

        async def fake_concat(seg_files, out):
            got["data"] = [f.read_bytes() for f in seg_files]
            Path(out).write_bytes(b"mp3")
            return True

        async def fake_probe(path):
            got["probed"] = Path(path)
            return 30.0
        monkeypatch.setattr(hls, "_concat_with_ffmpeg", fake_concat)
        monkeypatch.setattr(hls, "get_duration_ffprobe", fake_probe)
        out = tmp_path / "o.mp3"
        assert await hls.download_hls(BASE + "start.m3u8", out, headers={"Referer": "https://vk.com/"}, expected_duration=30)
        assert got["data"] == plains and got["probed"] == out
        assert key_hits == [1]                                   # cached across segments
        assert seen_referers == {"https://vk.com/"}               # headers on every request

    async def test_unfetchable_key_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hls.asyncio, "sleep", no_wait)
        m3u8 = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:1,\ns0.ts\n'

        def handler(req):
            name = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, text=m3u8) if name == "index.m3u8" else httpx.Response(404)
        real = httpx.AsyncClient
        monkeypatch.setattr(hls.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        assert not await hls.download_hls(BASE + "index.m3u8", tmp_path / "o.mp3")
