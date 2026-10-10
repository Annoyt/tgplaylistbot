"""YouTube search and download via yt-dlp."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
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


def ytdlp_error(stderr: bytes, limit: int = 300) -> str:
    """The actual ``ERROR:`` lines from yt-dlp stderr, for logging.

    A plain ``stderr[-500:]`` tail often showed only the end of a long URL and
    hid the reason. Each line is clipped so huge URLs can't drown it.
    """
    lines = stderr.decode(errors="replace").strip().splitlines()
    errors = [ln for ln in lines if ln.startswith("ERROR:")] or lines[-3:]
    return " | ".join(ln if len(ln) <= limit else ln[:limit] + "…" for ln in errors)


async def ytdlp_version() -> str:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() or "?"


async def _run(*cmd: str, timeout: float = 300) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b"", b"timed out"
    return proc.returncode, out, err


async def update_ytdlp() -> tuple[str, str] | None:
    """Upgrade yt-dlp if a newer release exists. Returns (old, new) or None.

    An in-place ``pip install -U`` deletes the yt-dlp entrypoint for a few
    seconds, failing any download started meanwhile. So the new version goes
    into a fresh venv and ``current`` is atomically repointed at it. The
    replaced venv is kept until the next upgrade, since a running yt-dlp may
    still import from it. Skipped when no yt-dlp dir is configured (local dev).
    """
    if not settings.ytdlp_dir:
        return None
    root = Path(settings.ytdlp_dir)
    current = root / "current"
    if not current.exists():
        return None

    spec = ["--pre"] if settings.ytdlp_channel == "nightly" else []  # nightlies are PyPI pre-releases
    spec.append("yt-dlp[default]")
    pip_flags = ["--no-cache-dir", "--disable-pip-version-check", "--quiet"]

    # Cheap check first: would pip upgrade yt-dlp itself in the live venv?
    rc, out, err = await _run(
        str(current / "bin" / "pip"), "install", "-U", "--dry-run", "--report", "-",
        *pip_flags, *spec,
    )
    if rc != 0:
        logger.warning("yt-dlp update check failed: %s", err.decode(errors="replace")[-300:])
        return None
    planned = json.loads(out or b"{}").get("install", [])
    if not any(p["metadata"]["name"].lower().replace("_", "-") == "yt-dlp" for p in planned):
        logger.info("yt-dlp is up to date (%s)", await ytdlp_version())
        return None

    old = await ytdlp_version()
    new_venv = root / f"v{int(time.time())}"
    rc, out, err = await _run(sys.executable, "-m", "venv", str(new_venv))
    if rc == 0:
        rc, out, err = await _run(str(new_venv / "bin" / "pip"), "install", *pip_flags, *spec)
    if rc == 0:
        rc, out, err = await _run(str(new_venv / "bin" / "yt-dlp"), "--version", timeout=60)
    if rc != 0:
        logger.warning("yt-dlp update failed: %s", err.decode(errors="replace")[-300:])
        shutil.rmtree(new_venv, ignore_errors=True)
        return None
    new = out.decode().strip()

    previous = current.resolve()
    tmp = root / "current.tmp"
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(new_venv.name)
    os.replace(tmp, current)  # atomic: a download sees the old or the new, never neither

    for d in root.iterdir():  # keep only the new venv and the one just replaced
        if d.is_dir() and not d.is_symlink() and d not in (new_venv, previous):
            shutil.rmtree(d, ignore_errors=True)
    logger.info("⬆️ yt-dlp updated: %s → %s", old, new)
    return old, new


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
                    # We download the best audio (opus format 251, ~160k). Show
                    # that as a realistic estimate — flat-playlist search carries
                    # no per-track bitrate, and probing each is slow/rate-limited.
                    bitrate=160,
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
            "--extractor-args", "youtube:player_client=default,android_vr",
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
            "--extractor-args", "youtube:player_client=default,android_vr",
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
        logger.error("yt-dlp download failed: %s", ytdlp_error(stderr))
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
        "--extractor-args", "youtube:player_client=default,android_vr",
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
        logger.error("yt-dlp URL download failed: %s", ytdlp_error(stderr))
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
        "--extractor-args", "youtube:player_client=default,android_vr",
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
        logger.error("yt-dlp video download failed: %s", ytdlp_error(stderr))
        return None

    # Output extension varies (mp4/mkv/webm); pick the produced file.
    for f in dl_dir.glob(f"{uid}.*"):
        if f.is_file() and f.stat().st_size > 0:
            return str(f)
    return None


async def probe_video_sizes(url: str) -> dict | None:
    """Estimate download size (bytes) for 360p / 720p / best of a video URL.

    One yt-dlp metadata call. Returns {'duration', '360', '720', 'best'} or None.
    Sizes may be 0 when the source doesn't report them (e.g. some HLS streams).
    """
    cmd = [
        "yt-dlp", "-J", "--no-warnings", "--no-playlist",
        "--user-agent", BROWSER_UA,
        "--extractor-args", "youtube:player_client=default,android_vr",
        "-4",
    ]
    cmd.extend(settings.cookie_args())
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout:
        return None

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError:
        return None

    formats = data.get("formats") or []

    def _fsize(f: dict) -> int:
        return int(f.get("filesize") or f.get("filesize_approx") or 0)

    audio = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    a_size = max((_fsize(f) for f in audio), default=0)

    def size_for(max_h: int | None) -> int:
        vids = [
            f for f in formats
            if f.get("vcodec") not in (None, "none")
            and (max_h is None or (f.get("height") or 0) <= max_h)
        ]
        if not vids:
            return 0
        best = max(vids, key=lambda f: (f.get("height") or 0))
        s = _fsize(best)
        if best.get("acodec") in (None, "none"):  # video-only → add audio
            s += a_size
        return s

    return {
        "duration": int(data.get("duration") or 0),
        "360": size_for(360),
        "720": size_for(720),
        "best": size_for(None),
    }

