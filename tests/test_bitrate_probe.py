"""services/bitrate_probe.py: real VK bitrate from the HLS stream."""

import asyncio

import httpx
import pytest

from services import bitrate_probe as bp

PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:20
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:1.000,
seg-1.ts
#EXTINF:4.000,
seg-2.ts
#EXTINF:20.000,
seg-3.ts
#EXTINF:3.000,
seg-4.ts
#EXTINF:20.000,
seg-5.ts
#EXTINF:2.500,
seg-6.ts
#EXT-X-ENDLIST
"""


class TestNominal:
    @pytest.mark.parametrize("measured,expected", [
        (331, 320), (352, 320), (265, 256), (199, 192), (133, 128), (167, 160), (0, 0), (-5, 0), (1000, 320),
    ])
    def test_snaps_to_standard_rates(self, measured, expected):
        assert bp.nominal_kbps(measured) == expected


class TestPickSegment:
    def test_shortest_full_segment_not_last(self):
        url, dur = bp.pick_segment(PLAYLIST, "https://cdn.example/a/b/index.m3u8?extra=1")
        assert (url, dur) == ("https://cdn.example/a/b/seg-4.ts", 3.0)   # 1 s is too short, 2.5 s is last

    def test_absolute_segment_urls_kept(self):
        playlist = "#EXTINF:4.0,\nhttps://other.example/x.ts\n#EXTINF:4.0,\nhttps://other.example/y.ts\n"
        assert bp.pick_segment(playlist, "https://cdn.example/i.m3u8") == ("https://other.example/x.ts", 4.0)

    def test_only_short_segments_still_usable(self):
        assert bp.pick_segment("#EXTINF:1.5,\na.ts\n", "https://h/i.m3u8") == ("https://h/a.ts", 1.5)

    def test_no_segments(self):
        assert bp.pick_segment("#EXTM3U\n", "https://h/i.m3u8") is None
        assert bp.pick_segment("#EXTINF:0,\na.ts\n", "https://h/i.m3u8") is None


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def cdn(ranges: bool, seg_bytes: int, seen=None):
    """A CDN host: playlist + segments; answers Range requests if ``ranges``."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.url.path, request.headers.get("range")))
        if request.url.path.endswith(".m3u8"):
            return httpx.Response(200, text=PLAYLIST)
        if request.url.path.endswith(".mp3"):
            if ranges:
                return httpx.Response(206, headers={"content-range": f"bytes 0-0/{seg_bytes}"}, content=b"x")
            return httpx.Response(200, content=b"x" * 10)
        if ranges and request.headers.get("range") == "bytes=0-0":
            return httpx.Response(206, headers={"content-range": f"bytes 0-0/{seg_bytes}"}, content=b"x")
        return httpx.Response(200, content=b"x" * seg_bytes)
    return handler


class TestProbe:
    async def test_range_answer_gives_size_without_download(self):
        seen = []
        async with client_for(cdn(True, 124_000, seen)) as c:     # 3 s segment, 124 KB → 331k → 320
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [320]
        assert seen == [("/a/index.m3u8", None), ("/a/seg-4.ts", "bytes=0-0")]

    async def test_full_segment_read_when_range_ignored(self):
        async with client_for(cdn(False, 72_000)) as c:            # 3 s, 72 KB → 192k
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [192]

    async def test_huge_segment_is_not_read_to_the_end(self):
        async with client_for(cdn(False, bp._MAX_READ + 70_000)) as c:
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [0]

    async def test_direct_mp3_uses_total_size(self):
        async with client_for(cdn(True, 8_000_000)) as c:           # 8 MB / 200 s = 320k
            assert await bp.probe_bitrates([("https://h/song.mp3?x=1", 200)], client=c) == [320]

    async def test_direct_mp3_is_never_downloaded_whole(self):
        async with client_for(cdn(False, 8_000_000)) as c:
            assert await bp.probe_bitrates([("https://h/song.mp3", 200)], client=c) == [0]

    async def test_direct_mp3_needs_a_duration(self):
        async with client_for(cdn(True, 8_000_000)) as c:
            assert await bp.probe_bitrates([("https://h/song.mp3", 0)], client=c) == [0]

    async def test_errors_and_missing_urls_are_zero_and_keep_positions(self):
        def handler(request):
            if "bad" in request.url.path:
                return httpx.Response(404)
            return cdn(True, 124_000)(request)
        async with client_for(handler) as c:
            got = await bp.probe_bitrates(
                [("", 200), ("https://h/bad/index.m3u8", 200), ("https://h/ok/index.m3u8", 200)], client=c)
        assert got == [0, 0, 320]

    async def test_playlist_without_segments(self):
        async with client_for(lambda r: httpx.Response(200, text="#EXTM3U\n")) as c:
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [0]

    async def test_slow_host_hits_the_shared_deadline(self):
        async def handler(request):
            if "slow" in request.url.path:
                await asyncio.sleep(5)
            return cdn(True, 124_000)(request)
        async with client_for(handler) as c:
            got = await bp.probe_bitrates(
                [("https://h/slow/index.m3u8", 200), ("https://h/fast/index.m3u8", 200)], timeout=0.3, client=c)
        assert got == [0, 320]

    async def test_nothing_to_probe(self):
        assert await bp.probe_bitrates([]) == []
        assert await bp.probe_bitrates([("", 100), ("", 0)]) == [0, 0]

    async def test_creates_its_own_client(self, monkeypatch):
        made = []
        real = httpx.AsyncClient

        def factory(**kw):
            made.append(kw)
            return real(transport=httpx.MockTransport(cdn(True, 124_000)), **kw)

        monkeypatch.setattr(bp.httpx, "AsyncClient", factory)
        assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], timeout=2.0) == [320]
        assert made[0]["follow_redirects"] is True and made[0]["timeout"].read == 2.0


# ── exact values and boundaries (written against surviving mutants) ────────

class TestExactPick:
    def test_one_second_bitrate_snaps_up(self):
        assert bp.nominal_kbps(1) == 64

    def test_one_second_segment_is_still_a_segment(self):
        assert bp.pick_segment("#EXTINF:1.0,\na.ts\n", "https://h/i.m3u8") == ("https://h/a.ts", 1.0)

    def test_second_to_last_can_be_picked(self):
        playlist = "#EXTINF:20,\na.ts\n#EXTINF:20,\nb.ts\n#EXTINF:3,\nc.ts\n#EXTINF:2.5,\nd.ts\n"
        assert bp.pick_segment(playlist, "https://h/i.m3u8") == ("https://h/c.ts", 3.0)

    def test_exactly_two_seconds_qualifies(self):
        playlist = "#EXTINF:20,\na.ts\n#EXTINF:2.0,\nb.ts\n#EXTINF:20,\nc.ts\n"
        assert bp.pick_segment(playlist, "https://h/i.m3u8") == ("https://h/b.ts", 2.0)

    def test_first_of_equal_segments(self):
        playlist = "#EXTINF:4,\nb.ts\n#EXTINF:4,\na.ts\n#EXTINF:9,\nz.ts\n"
        assert bp.pick_segment(playlist, "https://h/i.m3u8") == ("https://h/b.ts", 4.0)


class TestExactProbe:
    async def test_uses_get_with_a_one_byte_range(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.headers.get("range")))
            return cdn(True, 124_000)(request)
        async with client_for(handler) as c:
            await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c)
        assert seen == [("GET", None), ("GET", "bytes=0-0")]

    async def test_unknown_total_in_range_answer(self):
        def handler(request):
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(200, text=PLAYLIST)
            return httpx.Response(206, headers={"content-range": "bytes 0-0/*"}, content=b"x")
        async with client_for(handler) as c:
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [0]

    async def test_chunked_segment_is_summed(self):
        async def chunks():
            yield b"x" * 36_000
            yield b"x" * 36_000

        def handler(request):
            if request.url.path.endswith(".m3u8"):
                return httpx.Response(200, text=PLAYLIST)
            return httpx.Response(200, content=chunks())
        async with client_for(handler) as c:                     # 72 KB / 3 s → 192k
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [192]

    async def test_exactly_the_read_cap_is_measured(self):
        async with client_for(cdn(False, bp._MAX_READ)) as c:
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [320]

    async def test_segment_rounding_edge(self):
        # 55_650 B / 3 s = 148.4 kbps measured → ~140 without TS overhead → 128 (not 160)
        async with client_for(cdn(True, 55_650)) as c:
            assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c) == [128]

    async def test_mp3_rounding_edge_and_one_second(self):
        async with client_for(cdn(True, 3_710_000)) as c:           # 148.4 kbps over 200 s → 128
            assert await bp.probe_bitrates([("https://h/song.mp3", 200)], client=c) == [128]
        async with client_for(cdn(True, 42_400)) as c:              # 1 s, 339 kbps → 320
            assert await bp.probe_bitrates([("https://h/song.mp3", 1)], client=c) == [320]

    async def test_own_client_honours_the_deadline(self, monkeypatch):
        import time
        real = httpx.AsyncClient

        async def slow(request):
            await asyncio.sleep(3)
            return cdn(True, 124_000)(request)

        monkeypatch.setattr(bp.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(slow), **kw))
        start = time.monotonic()
        assert await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], timeout=0.2) == [0]
        assert time.monotonic() - start < 2

    async def test_summary_is_logged(self, caplog):
        async def handler(request):
            if "slow" in request.url.path:
                await asyncio.sleep(3)
            if "bad" in request.url.path:
                return httpx.Response(500)
            return cdn(True, 124_000)(request)
        items = [("https://h/ok/index.m3u8", 200), ("https://h/bad/index.m3u8", 200), ("https://h/slow/index.m3u8", 200)]
        with caplog.at_level("INFO", logger="services.bitrate_probe"):
            async with client_for(handler) as c:
                assert await bp.probe_bitrates(items, timeout=0.3, client=c) == [320, 0, 0]
        assert caplog.messages == ["VK bitrate probe: 1 ok, 1 failed, 1 timed out"]

    async def test_failure_without_timeouts_is_logged(self, caplog):
        def handler(request):
            return httpx.Response(500) if "bad" in request.url.path else cdn(True, 124_000)(request)
        with caplog.at_level("INFO", logger="services.bitrate_probe"):
            async with client_for(handler) as c:
                await bp.probe_bitrates([("https://h/bad/index.m3u8", 1), ("https://h/bad/x.m3u8", 1)], client=c)
        assert caplog.messages == ["VK bitrate probe: 0 ok, 2 failed, 0 timed out"]

    async def test_all_fine_logs_nothing(self, caplog):
        with caplog.at_level("INFO", logger="services.bitrate_probe"):
            async with client_for(cdn(True, 124_000)) as c:
                await bp.probe_bitrates([("https://h/a/index.m3u8", 200)], client=c)
        assert caplog.messages == []
