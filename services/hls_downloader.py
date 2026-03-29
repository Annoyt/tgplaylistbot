"""
Native HLS (m3u8) downloader for VK audio streams.

VK serves all audio as HLS streams. This module:
1. Parses the m3u8 master/media playlist
2. Downloads all .ts segments in parallel with retry logic
3. Concatenates segments via ffmpeg into a clean MP3
4. Validates the output duration

This replaces the broken approach of using httpx.get() on HLS URLs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# Max concurrent segment downloads
_MAX_CONCURRENCY = 8
# Retry attempts per segment
_MAX_RETRIES = 3
# Timeout per segment download (seconds)
_SEGMENT_TIMEOUT = 30
# Timeout for playlist fetch (seconds)
_PLAYLIST_TIMEOUT = 15


def _parse_m3u8(content: str, base_url: str) -> list[str]:
    """
    Parse an m3u8 playlist and return segment URLs.

    Handles both master playlists (selects highest bandwidth)
    and media playlists (returns segment list directly).
    """
    lines = content.strip().splitlines()

    if not lines or "#EXTM3U" not in lines[0]:
        logger.warning("Content is not a valid m3u8 playlist")
        return []

    # Check if this is a master playlist (contains #EXT-X-STREAM-INF)
    is_master = any("#EXT-X-STREAM-INF" in line for line in lines)

    if is_master:
        # Find the highest bandwidth variant
        best_bandwidth = -1
        best_url = None
        for i, line in enumerate(lines):
            if "#EXT-X-STREAM-INF" in line:
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                bandwidth = int(bw_match.group(1)) if bw_match else 0
                # Next non-comment line is the URL
                for j in range(i + 1, len(lines)):
                    candidate = lines[j].strip()
                    if candidate and not candidate.startswith("#"):
                        if bandwidth > best_bandwidth:
                            best_bandwidth = bandwidth
                            best_url = candidate
                        break

        if best_url:
            logger.info(f"Master playlist: selected variant with bandwidth={best_bandwidth}")
            return [_resolve_url(best_url, base_url)]
        logger.warning("Master playlist has no valid variants")
        return []

    # Media playlist — extract segment URLs
    segments = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            segments.append(_resolve_url(line, base_url))

    return segments


def _resolve_url(url: str, base_url: str) -> str:
    """Resolve a potentially relative URL against a base URL."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return urljoin(base_url, url)


def _get_base_url(url: str) -> str:
    """Extract the base URL for resolving relative paths."""
    # Remove query string for base URL calculation
    clean = url.split("?")[0]
    return clean.rsplit("/", 1)[0] + "/"


async def _fetch_playlist(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[str, str]:
    """
    Fetch an m3u8 playlist. Returns (content, final_url).
    The final_url may differ from the input due to redirects.
    """
    resp = await client.get(url, timeout=_PLAYLIST_TIMEOUT)
    resp.raise_for_status()
    final_url = str(resp.url)
    return resp.text, final_url


async def _download_segment(
    client: httpx.AsyncClient,
    url: str,
    output_path: Path,
    semaphore: asyncio.Semaphore,
    index: int,
) -> bool:
    """Download a single .ts segment with retry logic."""
    async with semaphore:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = await client.get(url, timeout=_SEGMENT_TIMEOUT)
                if resp.status_code == 200 and len(resp.content) > 0:
                    output_path.write_bytes(resp.content)
                    return True
                logger.warning(
                    f"Segment {index}: HTTP {resp.status_code}, "
                    f"size={len(resp.content)}, attempt {attempt}/{_MAX_RETRIES}"
                )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as e:
                logger.warning(
                    f"Segment {index}: {type(e).__name__} on attempt {attempt}/{_MAX_RETRIES}"
                )

            if attempt < _MAX_RETRIES:
                await asyncio.sleep(1.0 * attempt)

        logger.error(f"Segment {index}: FAILED after {_MAX_RETRIES} attempts")
        return False


async def _concat_with_ffmpeg(
    segment_files: list[Path],
    output_path: Path,
    to_mp3: bool = True,
) -> bool:
    """
    Concatenate .ts segments into a single file using ffmpeg.

    Uses the concat demuxer for reliable stitching.
    """
    if not segment_files:
        return False

    # Create concat list file
    list_path = segment_files[0].parent / f"concat_{uuid.uuid4().hex}.txt"
    try:
        with open(list_path, "w") as f:
            for seg in segment_files:
                # ffmpeg concat demuxer requires 'file' directive with escaped paths
                escaped = str(seg).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        if to_mp3:
            cmd = [
                "ffmpeg",
                "-y",                    # Overwrite output
                "-f", "concat",          # Use concat demuxer
                "-safe", "0",            # Allow absolute paths
                "-i", str(list_path),    # Input list
                "-c:a", "libmp3lame",    # Encode to MP3
                "-b:a", "320k",          # 320 kbps
                "-ar", "44100",          # 44.1 kHz sample rate
                "-ac", "2",              # Stereo
                "-write_xing", "1",      # Write VBR header for accurate duration
                str(output_path),
            ]
        else:
            # Copy without re-encoding (faster but less reliable for mixed segments)
            cmd = [
                "ffmpeg",
                "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_path),
                "-c", "copy",
                str(output_path),
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"ffmpeg concat failed: {stderr.decode()[-500:]}")
            return False

        return output_path.exists() and output_path.stat().st_size > 0

    finally:
        # Cleanup list file
        if list_path.exists():
            list_path.unlink()


async def get_duration_ffprobe(file_path: str | Path) -> float:
    """Get audio duration using ffprobe. Returns 0.0 on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return float(stdout.decode().strip())
    except Exception as e:
        logger.warning(f"ffprobe failed for {file_path}: {e}")
    return 0.0


async def download_hls(
    url: str,
    output_path: Path,
    headers: dict[str, str] | None = None,
    expected_duration: int = 0,
) -> bool:
    """
    Download an HLS stream to a local MP3 file.

    Args:
        url: The m3u8 playlist URL or a URL that redirects to one.
        output_path: Where to save the final MP3 file.
        headers: HTTP headers (User-Agent, Referer, etc.).
        expected_duration: Expected track duration for validation (0 = skip).

    Returns:
        True if download and conversion succeeded, False otherwise.
    """
    if headers is None:
        headers = {}

    work_dir = output_path.parent / f"hls_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)

    segment_files: list[Path] = []

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            timeout=_PLAYLIST_TIMEOUT,
        ) as client:

            # Step 1: Fetch the playlist
            logger.info(f"HLS: Fetching playlist from {url[:100]}...")
            playlist_content, final_url = await _fetch_playlist(client, url)
            base_url = _get_base_url(final_url)

            logger.debug(f"HLS: Playlist fetched ({len(playlist_content)} bytes), base_url={base_url[:80]}")

            # Step 2: Parse the playlist
            items = _parse_m3u8(playlist_content, base_url)

            if not items:
                logger.error("HLS: No segments found in playlist")
                return False

            # If we got a master playlist, the first item is a variant playlist URL
            # We need to fetch and parse that too
            if len(items) == 1 and items[0].endswith(".m3u8") or "m3u8" in items[0]:
                logger.info(f"HLS: Following variant playlist: {items[0][:100]}...")
                variant_content, variant_url = await _fetch_playlist(client, items[0])
                base_url = _get_base_url(variant_url)
                items = _parse_m3u8(variant_content, base_url)

                if not items:
                    logger.error("HLS: No segments in variant playlist")
                    return False

            logger.info(f"HLS: Found {len(items)} segments to download")

            # Step 3: Download all segments in parallel
            semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
            tasks = []

            for i, seg_url in enumerate(items):
                seg_path = work_dir / f"seg_{i:05d}.ts"
                segment_files.append(seg_path)
                tasks.append(
                    _download_segment(client, seg_url, seg_path, semaphore, i)
                )

            results = await asyncio.gather(*tasks)
            success_count = sum(1 for r in results if r)
            fail_count = len(results) - success_count

            logger.info(f"HLS: Downloaded {success_count}/{len(items)} segments ({fail_count} failed)")

            if success_count == 0:
                logger.error("HLS: All segment downloads failed!")
                return False

            if fail_count > 0:
                logger.warning(f"HLS: {fail_count} segments failed — output may have gaps")
                # Remove failed segments from the list so ffmpeg doesn't choke
                segment_files = [f for f in segment_files if f.exists() and f.stat().st_size > 0]

        # Step 4: Concatenate with ffmpeg
        logger.info(f"HLS: Concatenating {len(segment_files)} segments with ffmpeg...")
        ok = await _concat_with_ffmpeg(segment_files, output_path, to_mp3=True)

        if not ok:
            logger.error("HLS: ffmpeg concatenation failed")
            return False

        # Step 5: Validate duration
        if expected_duration > 0:
            real_duration = await get_duration_ffprobe(output_path)
            if real_duration > 0:
                diff = abs(real_duration - expected_duration)
                ratio = real_duration / expected_duration if expected_duration > 0 else 0
                if ratio < 0.8:
                    logger.warning(
                        f"HLS: Duration mismatch! Real={real_duration:.1f}s, "
                        f"Expected={expected_duration}s (ratio={ratio:.2f}). Track may be truncated."
                    )
                else:
                    logger.info(
                        f"HLS: Duration OK: {real_duration:.1f}s "
                        f"(expected {expected_duration}s, ratio={ratio:.2f})"
                    )

        file_size = output_path.stat().st_size
        logger.info(f"HLS: ✅ Complete! {file_size} bytes ({file_size / 1024 / 1024:.1f} MB)")
        return True

    except Exception as e:
        logger.error(f"HLS download failed: {e}", exc_info=True)
        return False

    finally:
        # Cleanup work directory
        for f in work_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            work_dir.rmdir()
        except OSError:
            pass


async def download_direct_streaming(
    url: str,
    output_path: Path,
    headers: dict[str, str] | None = None,
    max_size_bytes: int = 50 * 1024 * 1024,
) -> bool:
    """
    Download a direct audio URL using streaming (chunked) download.

    Unlike httpx.get() which loads everything into memory,
    this streams chunks to disk, which is more reliable for large files.
    """
    if headers is None:
        headers = {}

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            timeout=httpx.Timeout(connect=10, read=60, write=10, pool=10),
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.error(f"Direct download: HTTP {resp.status_code}")
                    return False

                content_type = resp.headers.get("content-type", "")
                logger.info(f"Direct download: Content-Type={content_type}")

                # Safety check: if server returns m3u8, abort and signal caller
                if "mpegurl" in content_type.lower() or "m3u8" in content_type.lower():
                    logger.warning("Direct download: Content-Type is m3u8! Need HLS downloader.")
                    return False

                total = 0
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > max_size_bytes:
                            logger.warning(f"Direct download: Exceeded max size ({max_size_bytes})")
                            return False
                        f.write(chunk)

                logger.info(f"Direct download: ✅ {total} bytes ({total / 1024 / 1024:.1f} MB)")
                return total > 0

    except Exception as e:
        logger.error(f"Direct download failed: {e}")
        return False


async def detect_url_type(
    url: str,
    headers: dict[str, str] | None = None,
) -> str:
    """
    Detect if a URL points to an HLS stream or a direct file.

    Returns:
        "hls" — URL is or redirects to an m3u8 playlist
        "direct" — URL is a direct audio file (mp3, aac, etc.)
        "unknown" — couldn't determine
    """
    if headers is None:
        headers = {}

    # Quick string check
    if "m3u8" in url or ".m3u8" in url:
        return "hls"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            timeout=15,
        ) as client:
            # HEAD request first
            resp = await client.head(url)
            final_url = str(resp.url)
            content_type = resp.headers.get("content-type", "").lower()

            # Check final URL after redirects
            if "m3u8" in final_url:
                return "hls"

            # Check Content-Type
            if "mpegurl" in content_type:
                return "hls"
            if "audio/" in content_type or "mpeg" in content_type:
                return "direct"
            if "octet-stream" in content_type:
                # Ambiguous — do a small GET to check content
                probe = await client.get(url, headers={"Range": "bytes=0-1023"})
                raw = probe.content
                try:
                    text = raw.decode("utf-8", errors="strict")
                    if "#EXTM3U" in text:
                        return "hls"
                except UnicodeDecodeError:
                    return "direct"

            # Fallback: probe content
            probe = await client.get(url, headers={"Range": "bytes=0-1023"})
            raw = probe.content
            try:
                text = raw.decode("utf-8", errors="strict")
                if "#EXTM3U" in text or "#EXT-X-" in text:
                    return "hls"
            except UnicodeDecodeError:
                return "direct"

    except Exception as e:
        logger.warning(f"URL type detection failed: {e}")

    return "unknown"
