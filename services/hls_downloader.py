"""
Native HLS downloader for VK audio streams.

VK serves audio as HLS with mixed encryption:
- Some segments are AES-128 encrypted
- Some are plaintext
- FFmpeg has trouble with VK's encryption — it silently drops encrypted 
  segment audio data, resulting in truncated tracks (~66% of expected length)

Solution: Download all segments + decryption keys ourselves,
decrypt AES-128 segments manually, then concat with ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import shutil
from pathlib import Path
from urllib.parse import urljoin
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import httpx

logger = logging.getLogger(__name__)

_MAX_CONCURRENCY = 8
_MAX_RETRIES = 3
_SEGMENT_TIMEOUT = 30
_PLAYLIST_TIMEOUT = 15


# ─── Data types ────────────────────────────────────────────────────────

class HLSSegment:
    """Represents a single HLS segment with optional encryption info."""
    __slots__ = ("url", "duration", "index", "key_url", "key_method", "iv")

    def __init__(self, url: str, duration: float, index: int,
                 key_url: str | None = None, key_method: str = "NONE",
                 iv: bytes | None = None):
        self.url = url
        self.duration = duration
        self.index = index
        self.key_url = key_url
        self.key_method = key_method
        self.iv = iv


# ─── Public API ────────────────────────────────────────────────────────

async def download_hls(
    url: str,
    output_path: Path,
    headers: dict[str, str] | None = None,
    expected_duration: int = 0,
) -> bool:
    """
    Download HLS stream with full AES-128 decryption support.
    
    1. Fetch & parse m3u8 playlist (follows master -> variant if needed)
    2. Download encryption keys
    3. Download all segments in parallel
    4. Decrypt AES-128 segments locally
    5. Concatenate with ffmpeg -> MP3 320kbps
    """
    if headers is None:
        headers = {}

    tmp_dir = Path(f"/tmp/hls_{uuid.uuid4().hex}")
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=headers, timeout=_PLAYLIST_TIMEOUT
        ) as client:
            # Step 1: Fetch playlist
            segments = await _resolve_segments(client, url)
            if not segments:
                logger.error("HLS: No segments found")
                return False

            total_m3u8_duration = sum(s.duration for s in segments)
            logger.info(
                f"HLS: {len(segments)} segments, "
                f"m3u8 duration={total_m3u8_duration:.1f}s, "
                f"VK duration={expected_duration}s"
            )

            # Step 2: Download encryption keys
            key_cache: dict[str, bytes] = {}
            for seg in segments:
                if seg.key_method == "AES-128" and seg.key_url and seg.key_url not in key_cache:
                    key_data = await _fetch_key(client, seg.key_url)
                    if key_data:
                        key_cache[seg.key_url] = key_data
                        logger.debug(f"HLS: Cached key for {seg.key_url[:60]}...")
                    else:
                        logger.error(f"HLS: Failed to fetch key: {seg.key_url[:80]}")
                        return False

            # Step 3: Download all segments in parallel
            semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
            tasks = []
            for seg in segments:
                seg_path = tmp_dir / f"seg_{seg.index:05d}.ts"
                tasks.append(_download_and_decrypt_segment(
                    client, seg, seg_path, key_cache, semaphore
                ))

            results = await asyncio.gather(*tasks)
            success = sum(1 for r in results if r)
            failed = len(results) - success
            logger.info(f"HLS: Downloaded {success}/{len(segments)} segments ({failed} failed)")

            if success == 0:
                return False

        # Step 4: Collect successfully downloaded segment files
        seg_files = sorted([
            f for f in tmp_dir.glob("seg_*.ts")
            if f.stat().st_size > 0
        ])

        if not seg_files:
            logger.error("HLS: No segment files after download")
            return False

        # Step 5: Concatenate with ffmpeg
        ok = await _concat_with_ffmpeg(seg_files, output_path)
        if not ok:
            logger.error("HLS: ffmpeg concatenation failed")
            return False

        # Step 6: Validate
        real_dur = await get_duration_ffprobe(output_path)
        fsize = output_path.stat().st_size
        logger.info(f"HLS: ✅ Done! {fsize/1024/1024:.1f} MB, {real_dur:.1f}s")

        if expected_duration > 0 and real_dur > 0:
            ratio = real_dur / expected_duration
            if ratio < 0.9:
                logger.warning(
                    f"HLS: Possible truncation — real={real_dur:.1f}s, "
                    f"expected={expected_duration}s (ratio={ratio:.2f})"
                )

        return True

    except Exception as e:
        logger.error(f"HLS download failed: {e}", exc_info=True)
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def download_direct_streaming(
    url: str,
    output_path: Path,
    headers: dict[str, str] | None = None,
    max_size_bytes: int = 50 * 1024 * 1024,
) -> bool:
    """Stream download for direct (non-HLS) audio files."""
    if headers is None:
        headers = {}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=headers, timeout=60
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return False
                ctype = resp.headers.get("content-type", "").lower()
                if "mpegurl" in ctype or "m3u8" in ctype:
                    return False
                total = 0
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=128 * 1024):
                        total += len(chunk)
                        if total > max_size_bytes:
                            return False
                        f.write(chunk)
                return total > 0
    except Exception:
        return False


async def detect_url_type(url: str, headers: dict[str, str] | None = None) -> str:
    """Detect if URL is HLS or direct audio."""
    if ".m3u8" in url.split("?")[0]:
        return "hls"
    if headers is None:
        headers = {}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=headers, timeout=10
        ) as client:
            resp = await client.head(url)
            ctype = resp.headers.get("content-type", "").lower()
            if "mpegurl" in ctype:
                return "hls"
            if "audio/" in ctype or "mpeg" in ctype:
                return "direct"
            # Probe content
            probe = await client.get(url, headers={"Range": "bytes=0-512"})
            if "#EXTM3U" in probe.text:
                return "hls"
            return "direct"
    except Exception:
        return "unknown"


async def get_duration_ffprobe(path: Path | str) -> float:
    """Get audio duration via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return float(out.decode().strip()) if out.strip() else 0.0
    except Exception:
        return 0.0


# ─── Internal helpers ──────────────────────────────────────────────────

async def _resolve_segments(
    client: httpx.AsyncClient, url: str
) -> list[HLSSegment]:
    """Fetch and parse m3u8 — follows master playlist if needed."""
    resp = await client.get(url)
    resp.raise_for_status()
    content = resp.text
    base_url = _get_base_url(str(resp.url))

    # Check for master playlist
    if "#EXT-X-STREAM-INF" in content:
        # Pick the best variant (highest bandwidth)
        best_bw = -1
        best_variant = None
        lines = content.strip().splitlines()
        for i, line in enumerate(lines):
            if "#EXT-X-STREAM-INF" in line:
                bw_m = re.search(r"BANDWIDTH=(\d+)", line)
                bw = int(bw_m.group(1)) if bw_m else 0
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith("#"):
                        if bw > best_bw:
                            best_bw = bw
                            best_variant = _resolve_url(nxt, base_url)
                        break

        if best_variant:
            logger.info(f"HLS: Master playlist → variant (bw={best_bw})")
            resp2 = await client.get(best_variant)
            resp2.raise_for_status()
            content = resp2.text
            base_url = _get_base_url(str(resp2.url))
        else:
            logger.warning("HLS: Master playlist but no variant found")
            return []

    return _parse_media_playlist(content, base_url)


def _parse_media_playlist(content: str, base_url: str) -> list[HLSSegment]:
    """Parse a media m3u8 playlist into HLSSegment objects with encryption info."""
    lines = content.strip().splitlines()
    segments: list[HLSSegment] = []

    current_key_method = "NONE"
    current_key_url: str | None = None
    current_iv: bytes | None = None
    seg_index = 0

    for i, line in enumerate(lines):
        line = line.strip()

        # Track encryption changes
        if line.startswith("#EXT-X-KEY"):
            method_m = re.search(r"METHOD=([A-Z0-9-]+)", line)
            uri_m = re.search(r'URI="([^"]+)"', line)
            iv_m = re.search(r"IV=0x([0-9a-fA-F]+)", line)

            current_key_method = method_m.group(1) if method_m else "NONE"
            current_key_url = _resolve_url(uri_m.group(1), base_url) if uri_m else None
            current_iv = bytes.fromhex(iv_m.group(1)) if iv_m else None

            if current_key_method == "NONE":
                current_key_url = None
                current_iv = None

        elif line.startswith("#EXTINF:"):
            dur_m = re.search(r"#EXTINF:([\d.]+)", line)
            duration = float(dur_m.group(1)) if dur_m else 0.0

            # Next non-comment line is the segment URL
            for j in range(i + 1, len(lines)):
                seg_line = lines[j].strip()
                if seg_line and not seg_line.startswith("#"):
                    seg_url = _resolve_url(seg_line, base_url)

                    # Default IV = segment sequence number (big-endian 16 bytes)
                    iv = current_iv
                    if current_key_method == "AES-128" and iv is None:
                        iv = seg_index.to_bytes(16, byteorder="big")

                    segments.append(HLSSegment(
                        url=seg_url,
                        duration=duration,
                        index=seg_index,
                        key_url=current_key_url,
                        key_method=current_key_method,
                        iv=iv,
                    ))
                    seg_index += 1
                    break

    return segments


async def _fetch_key(client: httpx.AsyncClient, key_url: str) -> bytes | None:
    """Fetch an AES-128 key (16 bytes)."""
    for attempt in range(_MAX_RETRIES):
        try:
            resp = await client.get(key_url, timeout=10)
            if resp.status_code == 200 and len(resp.content) == 16:
                return resp.content
            logger.warning(f"Key fetch: status={resp.status_code}, size={len(resp.content)}")
        except Exception as e:
            logger.warning(f"Key fetch error (attempt {attempt+1}): {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def _download_and_decrypt_segment(
    client: httpx.AsyncClient,
    seg: HLSSegment,
    output_path: Path,
    key_cache: dict[str, bytes],
    semaphore: asyncio.Semaphore,
) -> bool:
    """Download a segment, decrypt if needed, save to disk."""
    async with semaphore:
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await client.get(seg.url, timeout=_SEGMENT_TIMEOUT)
                if resp.status_code != 200 or not resp.content:
                    logger.warning(f"Seg {seg.index}: HTTP {resp.status_code}")
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue

                data = resp.content

                # Decrypt AES-128 if needed
                if seg.key_method == "AES-128" and seg.key_url:
                    key = key_cache.get(seg.key_url)
                    if not key:
                        logger.error(f"Seg {seg.index}: No key available")
                        return False

                    iv = seg.iv or seg.index.to_bytes(16, byteorder="big")
                    try:
                        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                        decryptor = cipher.decryptor()
                        data = decryptor.update(data) + decryptor.finalize()

                        # Remove PKCS7 padding
                        if data:
                            pad_len = data[-1]
                            if 1 <= pad_len <= 16 and all(b == pad_len for b in data[-pad_len:]):
                                data = data[:-pad_len]
                    except Exception as e:
                        logger.error(f"Seg {seg.index}: Decryption failed: {e}")
                        return False

                output_path.write_bytes(data)
                return True

            except Exception as e:
                logger.warning(f"Seg {seg.index}: {type(e).__name__} (attempt {attempt+1})")
                await asyncio.sleep(0.5 * (attempt + 1))

    logger.error(f"Seg {seg.index}: FAILED after {_MAX_RETRIES} attempts")
    return False


async def _concat_with_ffmpeg(seg_files: list[Path], output_path: Path) -> bool:
    """Concatenate decrypted .ts segments into MP3 using ffmpeg."""
    list_path = seg_files[0].parent / "concat.txt"
    with open(list_path, "w") as f:
        for seg in seg_files:
            escaped = str(seg.absolute()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:a", "libmp3lame", "-b:a", "320k",
        "-ar", "44100", "-ac", "2",
        "-write_xing", "1",
        str(output_path),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"ffmpeg concat failed: {stderr.decode()[-500:]}")
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def _resolve_url(url: str, base_url: str) -> str:
    if url.startswith("http"):
        return url
    return urljoin(base_url, url)


def _get_base_url(url: str) -> str:
    clean = url.split("?")[0]
    return clean.rsplit("/", 1)[0] + "/"
