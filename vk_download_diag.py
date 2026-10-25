"""
VK Download Diagnostics — trace the full download pipeline.
Usage: python vk_download_diag.py [search_query]
"""

import asyncio
import logging
import sys
import os
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("vk_diag")

# Suppress noisy loggers
for noisy in ("httpx", "httpcore", "hpack", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def main():
    query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Nirvana Smells Like Teen Spirit"
    logger.info(f"=== VK Download Diagnostics ===")
    logger.info(f"Query: {query}")

    # 1. Init DB
    from app.db.database import init_db
    await init_db()

    # 2. Search
    from services import vk_music
    logger.info("--- Step 1: Search ---")
    results = await vk_music.search(query, count=3)
    if not results:
        logger.error("No results found! Check VK credentials.")
        return

    for i, t in enumerate(results):
        logger.info(f"  [{i}] {t.artist} - {t.title} ({t.duration}s) source_id={t.source_id}")

    track = results[0]
    logger.info(f"\n--- Step 2: URL Resolution for '{track.artist} - {track.title}' ---")

    # 3. Resolve URL
    import re
    import httpx
    from services.vk_music import _get_vk_creds, _init_vk_audio, OFFICIAL_UA

    dl_url = track.source_id
    creds = await _get_vk_creds()

    if "vk.com/audio" in dl_url and "mp3" not in dl_url:
        match = re.search(r"audio(-?\d+)_(\d+)", dl_url)
        if match:
            owner_id, audio_id = match.groups()
            loop = asyncio.get_event_loop()
            vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds, None)
            if vk_audio:
                def _get_url():
                    try:
                        res = vk_audio._vk.method("audio.getById", {
                            "audios": f"{owner_id}_{audio_id}"
                        })
                        return res[0] if res else None
                    except Exception as e:
                        logger.error(f"audio.getById failed: {e}")
                        return None

                audio_data = await loop.run_in_executor(None, _get_url)
                if audio_data:
                    resolved_url = audio_data.get("url", "")
                    logger.info(f"  Resolved URL: {resolved_url[:200]}...")
                    logger.info(f"  Full audio data keys: {list(audio_data.keys())}")
                    logger.info(f"  is_hq: {audio_data.get('is_hq')}")
                    logger.info(f"  content_restricted: {audio_data.get('content_restricted')}")
                    dl_url = resolved_url
                else:
                    logger.error("  audio.getById returned empty!")
                    return

    logger.info(f"\n--- Step 3: URL Analysis ---")
    logger.info(f"  Final URL: {dl_url[:300]}")
    logger.info(f"  Contains 'm3u8': {'m3u8' in dl_url}")
    logger.info(f"  Contains '.mp3': {'.mp3' in dl_url}")
    logger.info(f"  Contains '.ts': {'.ts' in dl_url}")

    # 4. HEAD request to check Content-Type
    logger.info(f"\n--- Step 4: HEAD Request ---")
    headers = {"User-Agent": OFFICIAL_UA}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            head_resp = await client.head(dl_url)
            logger.info(f"  Status: {head_resp.status_code}")
            logger.info(f"  Content-Type: {head_resp.headers.get('content-type', 'N/A')}")
            logger.info(f"  Content-Length: {head_resp.headers.get('content-length', 'N/A')}")
            logger.info(f"  Final URL (after redirects): {head_resp.url}")
            
            final_url = str(head_resp.url)
            logger.info(f"  Final URL contains 'm3u8': {'m3u8' in final_url}")
    except Exception as e:
        logger.error(f"  HEAD request failed: {e}")
        final_url = dl_url

    # 5. Attempt raw download (first 4KB to inspect content)
    logger.info(f"\n--- Step 5: Raw Content Probe (first 4KB) ---")
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
            resp = await client.get(dl_url, headers={"Range": "bytes=0-4095"})
            raw = resp.content
            logger.info(f"  Status: {resp.status_code}")
            logger.info(f"  Content-Type: {resp.headers.get('content-type', 'N/A')}")
            logger.info(f"  Received {len(raw)} bytes")

            # Check if it's text (m3u8 playlist) or binary (actual audio)
            try:
                text = raw.decode("utf-8", errors="strict")
                if "#EXTM3U" in text or "#EXT-X-" in text:
                    logger.info(f"  ⚠️ Content IS an m3u8 playlist!")
                    logger.info(f"  First 500 chars:\n{text[:500]}")
                else:
                    logger.info(f"  Content is text but NOT m3u8. First 200 chars:\n{text[:200]}")
            except UnicodeDecodeError:
                # Binary content = likely actual audio
                # Check MP3 magic bytes
                if raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb" or raw[:2] == b"\xff\xf3":
                    logger.info(f"  ✅ Content IS an MP3 file (magic bytes detected)")
                elif raw[:4] == b"\x00\x00\x00\x1c" or raw[:4] == b"ftyp":
                    logger.info(f"  Content is MP4/AAC container")
                else:
                    logger.info(f"  Content is binary, first 16 bytes: {raw[:16].hex()}")

    except Exception as e:
        logger.error(f"  Probe failed: {e}")

    # 6. Full download test (using current method)
    logger.info(f"\n--- Step 6: Full Download Test ---")
    dl_dir = Path("/tmp/musicbot_diag")
    dl_dir.mkdir(parents=True, exist_ok=True)

    file_path = await vk_music.download(track, download_dir=dl_dir)

    if file_path and os.path.exists(file_path):
        file_size = os.path.getsize(file_path)
        logger.info(f"  ✅ Downloaded: {file_path}")
        logger.info(f"  File size: {file_size} bytes ({file_size / 1024:.1f} KB)")

        # ffprobe check
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,format_name,bit_rate",
            "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
            "-of", "json",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            import json
            info = json.loads(stdout.decode())
            logger.info(f"  ffprobe info: {json.dumps(info, indent=2)}")

            real_duration = float(info.get("format", {}).get("duration", 0))
            expected_duration = track.duration
            diff = abs(real_duration - expected_duration)
            if diff > 5:
                logger.warning(f"  ⚠️ DURATION MISMATCH! Real={real_duration:.1f}s, Expected={expected_duration}s, Diff={diff:.1f}s")
            else:
                logger.info(f"  ✅ Duration OK: Real={real_duration:.1f}s, Expected={expected_duration}s")
        else:
            logger.error(f"  ffprobe failed: {stderr.decode()}")

        # Cleanup
        os.remove(file_path)
    else:
        logger.error(f"  ❌ Download FAILED!")

    # 7. Also test with yt-dlp for comparison
    logger.info(f"\n--- Step 7: yt-dlp Download Test ---")
    if "m3u8" in dl_url or "m3u8" in final_url:
        uid = uuid.uuid4().hex
        ytdl_output = str(dl_dir / f"{uid}.%(ext)s")
        cmd = [
            "yt-dlp", "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--user-agent", OFFICIAL_UA,
            "--hls-prefer-native",
            "--fragment-retries", "10",
            "--retries", "3",
            "-o", ytdl_output,
            "-v",
            dl_url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        logger.info(f"  yt-dlp exit code: {proc.returncode}")
        # Show last 30 lines of output
        lines = stdout.decode().strip().split("\n")
        for line in lines[-30:]:
            logger.info(f"  yt-dlp: {line}")

        ytdl_file = dl_dir / f"{uid}.mp3"
        if ytdl_file.exists():
            logger.info(f"  ✅ yt-dlp download: {ytdl_file.stat().st_size} bytes")
            os.remove(ytdl_file)
        else:
            logger.warning(f"  ❌ yt-dlp output file not found")
    else:
        logger.info(f"  Skipped — URL is not m3u8")

    logger.info("\n=== Diagnostics Complete ===")


if __name__ == "__main__":
    asyncio.run(main())
