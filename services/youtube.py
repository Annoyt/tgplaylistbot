"""YouTube search and download via yt-dlp."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)


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
        "-o", output_template,
        "--max-filesize", f"{settings.max_file_size_mb}m",
    ]
    if os.path.exists("cookies.txt"):
        cmd.extend(["--cookies", "cookies.txt"])
    cmd.append(url)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("yt-dlp URL download failed: %s", stderr.decode())
        return None

    out_file = dl_dir / f"{uid}.mp3"
    if out_file.exists() and out_file.stat().st_size > 0:
        return str(out_file)
    return None
