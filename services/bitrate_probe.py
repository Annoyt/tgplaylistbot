"""Real bitrate of VK audio, cheap enough to run on search results.

VK's search API carries no quality field (``is_hq`` is gone, so every VK track
used to be shown and ranked as 128k), yet actual copies range from 128k to
320k. Each result's ``url`` is an HLS playlist; the bitrate is the byte size of
one segment over its duration. Measured on prod 2026-11-08: within 2% of a
full-segment measurement, ~0.6 s for 10 tracks in parallel; the CDN answers a
1-byte Range request on some hosts, others send the segment (≤ ~160 KB for the
shortest one), so a probe costs a few KB to ~160 KB.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

STANDARD_KBPS = (64, 96, 128, 160, 192, 224, 256, 320)
# MPEG-TS packaging adds ~3-9% on top of the audio bitrate (320k measures 331-352).
_TS_OVERHEAD = 1.06
PROBE_TIMEOUT_SEC = 3.0
_MAX_PARALLEL = 10
_MAX_READ = 2 * 1024 * 1024  # never pull more than this for one probe

_SEGMENT_RE = re.compile(r"#EXTINF:\s*([\d.]+)[^\n]*\n\s*([^\n#][^\n]*)")


def nominal_kbps(measured: float) -> int:
    """Closest standard bitrate to a measured one (0 if unknown)."""
    if measured <= 0:
        return 0
    est = measured / _TS_OVERHEAD
    return min(STANDARD_KBPS, key=lambda k: abs(k - est))


def pick_segment(playlist: str, base_url: str) -> tuple[str, float] | None:
    """URL and duration of the shortest full segment (fewest bytes to fetch).

    The last segment is skipped (often a short stub) as are those under 2 s,
    where container overhead skews the estimate.
    """
    segs = [(float(d), name.strip()) for d, name in _SEGMENT_RE.findall(playlist)]
    segs = [s for s in segs if s[0] > 0]
    if not segs:
        return None
    candidates = [s for s in segs[:-1] if s[0] >= 2.0] or segs
    dur, name = min(candidates, key=lambda s: s[0])
    return urljoin(base_url, name), dur


async def _resource_size(client: httpx.AsyncClient, url: str, *, allow_full: bool) -> int:
    """Byte size of ``url``: from a 1-byte Range answer, else by reading it."""
    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as r:
        r.raise_for_status()
        if r.status_code == 206:
            total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
            return int(total) if total.isdigit() else 0
        if not allow_full:
            return 0
        n = 0
        async for chunk in r.aiter_bytes():
            n += len(chunk)
            if n > _MAX_READ:
                return 0
        return n


async def _probe_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str, duration: int) -> int:
    async with sem:
        if urlparse(url).path.endswith(".mp3"):
            # A whole file: its size over the track length. Only via Range —
            # never download the full track just to rank it.
            if duration <= 0:
                return 0
            size = await _resource_size(client, url, allow_full=False)
            return nominal_kbps(size * 8 / duration / 1000)
        r = await client.get(url)
        r.raise_for_status()
        seg = pick_segment(r.text, str(r.url))
        if seg is None:
            return 0
        size = await _resource_size(client, seg[0], allow_full=True)
        return nominal_kbps(size * 8 / seg[1] / 1000)


async def probe_bitrates(
    items: list[tuple[str, int]],
    timeout: float = PROBE_TIMEOUT_SEC,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[int]:
    """Nominal kbps for each ``(url, duration)``; 0 where unknown or too slow.

    The whole batch shares one deadline, so a slow CDN host costs at most
    ``timeout`` seconds of search time and only its own tracks stay unknown.
    """
    if not any(url for url, _ in items):
        return [0] * len(items)
    if client is None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=True) as own:
            return await _probe_all(own, items, timeout)
    return await _probe_all(client, items, timeout)


async def _probe_all(client: httpx.AsyncClient, items: list[tuple[str, int]], timeout: float) -> list[int]:
    results = [0] * len(items)
    sem = asyncio.Semaphore(_MAX_PARALLEL)
    tasks = {
        asyncio.create_task(_probe_one(client, sem, url, duration)): i
        for i, (url, duration) in enumerate(items) if url
    }
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    failed = 0
    for t in done:
        if t.exception() is None:
            results[tasks[t]] = t.result()
        else:
            failed += 1
    if pending or failed:
        logger.info("VK bitrate probe: %d ok, %d failed, %d timed out",
                    len(done) - failed, failed, len(pending))
    return results
