"""YouTube search and download via yt-dlp."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)

# Realistic desktop browser UA. Instagram's web extractor serves blocked/login
# pages to non-browser agents, so we mimic Chrome alongside the cookies.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def search(query: str, count: int = 30) -> list[TrackInfo]:
    """Search YouTube for audio tracks."""
    cmd = [
        "yt-dlp",
        f"ytsearch{count}:{query}",
        "--dump-json",
        "--flat-playlist",
        "--no-download",
        "--quiet",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()

    tracks: list[TrackInfo] = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
            duration = int(data.get("duration") or 0)
            tracks.append(
                TrackInfo(
                    title=data.get("title", "Unknown"),
                    artist=data.get("uploader", data.get("channel", "Unknown")),
                    duration=duration,
                    source="youtube",
                    source_id=data.get("id", data.get("url", "")),
                    bitrate=0,
                    filesize=0,
                )
            )
        except (json.JSONDecodeError, KeyError):
            continue
    return tracks


async def download(
    track: TrackInfo,
    quality: str = "mp3_320",
    download_dir: Path | None = None,
) -> str | None:
    """Download audio from YouTube. Returns path to file or None."""
    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://www.youtube.com/watch?v={track.source_id}"
    uid = uuid.uuid4().hex
    output_template = str(dl_dir / f"{uid}.%(ext)s")

    if quality == "flac":
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "flac",
            "--audio-quality", "0",
            "--user-agent", BROWSER_UA,
            "--extractor-args", "youtube:player_client=android_vr,android,ios",
            "-4",
            "-o", output_template,
            "--max-filesize", f"{settings.max_file_size_mb}m",
            url,
        ]
    else:
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--postprocessor-args", "ffmpeg:-b:a 320k",
            "--user-agent", BROWSER_UA,
            "--extractor-args", "youtube:player_client=android_vr,android,ios",
            "-4",
            "-o", output_template,
            "--max-filesize", f"{settings.max_file_size_mb}m",
            url,
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("yt-dlp download failed: %s", stderr.decode())
        return None

    ext = "flac" if quality == "flac" else "mp3"
    out_file = dl_dir / f"{uid}.{ext}"
    if out_file.exists() and out_file.stat().st_size > 0:
        return str(out_file)
    return None


async def download_from_url(url: str, download_dir: Path | None = None) -> str | None:
    """Download audio from any URL supported by yt-dlp (Instagram, TikTok, etc.)."""
    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    uid = uuid.uuid4().hex
    output_template = str(dl_dir / f"{uid}.%(ext)s")
    
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--hls-prefer-native",
        "--fragment-retries", "10",
        "--retries", "3",
        "--no-part",
        "--fixup", "detect_or_warn",
        "--user-agent", BROWSER_UA,
        "--extractor-args", "youtube:player_client=android_vr,android,ios",
        "-4",
        "-o", output_template,
        "--max-filesize", f"{settings.max_file_size_mb}m",
    ]
    cmd.extend(settings.cookie_args())
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("yt-dlp URL download failed: %s", stderr.decode()[-500:])
        return None

    out_file = dl_dir / f"{uid}.mp3"
    if out_file.exists() and out_file.stat().st_size > 0:
        return str(out_file)
    return None


async def download_video_from_url(
    url: str,
    max_height: int | None = None,
    download_dir: Path | None = None,
) -> str | None:
    """Download the full VIDEO (not just audio) from a URL via yt-dlp.

    Reels/TikTok/Shorts/YouTube etc. ``max_height`` caps the resolution
    (e.g. 720); ``None`` means best available. Returns the file path or None.
    """
    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    uid = uuid.uuid4().hex
    output_template = str(dl_dir / f"{uid}.%(ext)s")

    if max_height:
        # Best video+audio merge ≤ height, else best combined ≤ height, else best.
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    else:
        fmt = "bv*+ba/b"

    limit_mb = settings.send_limit_bytes // (1024 * 1024)
    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--fragment-retries", "10",
        "--retries", "3",
        "--no-part",
        "--user-agent", BROWSER_UA,
        "--extractor-args", "youtube:player_client=android_vr,android,ios",
        "-4",
        "-o", output_template,
        "--max-filesize", f"{limit_mb}m",
    ]
    cmd.extend(settings.cookie_args())
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("yt-dlp video download failed: %s", stderr.decode()[-500:])
        return None

    # Output extension varies (mp4/mkv/webm); pick the produced file.
    for f in dl_dir.glob(f"{uid}.*"):
        if f.is_file() and f.stat().st_size > 0:
            return str(f)
    return None

