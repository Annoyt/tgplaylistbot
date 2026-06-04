"""Audiobook search & download across pluggable providers.

Each provider exposes ``search(query, count)`` and a ``download(track, dir)``
entry; the public ``search()`` / ``download()`` here fan out / dispatch so new
sources (extra sites, torrents) can be added without touching the handlers.

Phase 1 providers: YouTube (yt-dlp) and LibriVox (public-domain, official API).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from difflib import SequenceMatcher
from glob import glob
from pathlib import Path

import httpx

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)

# A result shorter than this is treated as an excerpt/clip, not a full book.
_MIN_AUDIOBOOK_SEC = 12 * 60

_LIBRIVOX_API = "https://librivox.org/api/feed/audiobooks"


# ──────────────────────────────────────────────────────────────────────────
# Ranking helpers (books want the longest *relevant* match, not a short one)
# ──────────────────────────────────────────────────────────────────────────
_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
# Words that appear in audiobook titles but aren't part of the work's name.
_STOP = ("аудиокнига", "аудио", "книга", "audiobook", "full", "полная", "версия",
         "слушать", "онлайн", "official", "channel")


def _normalize(s: str) -> str:
    s = _NOISE_RE.sub(" ", s.lower())
    s = _NON_WORD_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    for w in _STOP:
        s = s.replace(w, " ")
    return _WS_RE.sub(" ", s).strip()


def _rank(tracks: list[TrackInfo], query: str) -> list[TrackInfo]:
    """Order by title closeness to the query, gently favouring longer books."""
    q = _normalize(query)
    longest = max((t.duration for t in tracks), default=0) or 1

    def key(t: TrackInfo) -> float:
        text = _normalize(f"{t.artist} {t.title}")
        sim = SequenceMatcher(None, q, _normalize(t.title)).ratio()
        if q and q in text:
            sim += 0.15
        # Up to +0.15 for being among the longer (more complete) results.
        sim += 0.15 * (t.duration / longest)
        return sim

    return sorted(tracks, key=key, reverse=True)


# ──────────────────────────────────────────────────────────────────────────
# Provider: YouTube
# ──────────────────────────────────────────────────────────────────────────
async def _yt_search(query: str, count: int = 15) -> list[TrackInfo]:
    """Search YouTube biased toward full audiobooks; keep long results only."""
    cmd = [
        "yt-dlp",
        f"ytsearch{count}:{query} аудиокнига",
        "--dump-json",
        "--flat-playlist",
        "--no-download",
        "--quiet",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    stdout, _ = await proc.communicate()

    tracks: list[TrackInfo] = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        duration = int(data.get("duration") or 0)
        if duration < _MIN_AUDIOBOOK_SEC:
            continue
        tracks.append(
            TrackInfo(
                title=data.get("title", "Unknown"),
                artist=data.get("uploader") or data.get("channel") or "Unknown",
                duration=duration,
                source="youtube",
                source_id=data.get("id") or data.get("url") or "",
                bitrate=0,
                filesize=0,
            )
        )
    return tracks


async def _yt_download(track: TrackInfo, dl_dir: Path) -> str | None:
    uid = uuid.uuid4().hex
    out_tmpl = str(dl_dir / f"{uid}.%(ext)s")
    url = f"https://www.youtube.com/watch?v={track.source_id}"
    # No --max-filesize: audiobooks are large by design. Re-encode to mono speech.
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--postprocessor-args", f"ffmpeg:-b:a {settings.audiobook_bitrate_kbps}k -ac 1",
        "--hls-prefer-native",
        "--fragment-retries", "15",
        "--retries", "5",
        "--no-part",
        "-o", out_tmpl,
    ]
    cmd.extend(settings.cookie_args())
    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Audiobook YT download failed: %s", stderr.decode()[-500:])
        return None

    out = dl_dir / f"{uid}.mp3"
    return str(out) if out.exists() and out.stat().st_size > 0 else None


# ──────────────────────────────────────────────────────────────────────────
# Provider: LibriVox (free public-domain audiobooks, official JSON API)
# ──────────────────────────────────────────────────────────────────────────
async def _librivox_search(query: str, count: int = 8) -> list[TrackInfo]:
    params = {"title": query, "format": "json", "extended": "1", "limit": str(count)}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_LIBRIVOX_API, params=params)
            if resp.status_code != 200:
                return []
            books = resp.json().get("books", [])
    except Exception as e:
        logger.warning("LibriVox search failed: %s", e)
        return []

    tracks: list[TrackInfo] = []
    for b in books:
        duration = int(b.get("totaltimesecs") or 0)
        if duration < _MIN_AUDIOBOOK_SEC:
            continue
        authors = b.get("authors") or []
        author = "Unknown"
        if authors:
            a = authors[0]
            author = f"{a.get('first_name','')} {a.get('last_name','')}".strip() or "Unknown"
        tracks.append(
            TrackInfo(
                title=b.get("title", "Unknown"),
                artist=author,
                duration=duration,
                source="librivox",
                source_id=str(b.get("id", "")),
                bitrate=0,
                filesize=0,
            )
        )
    return tracks


async def _librivox_sections(book_id: str) -> list[str]:
    """Return ordered chapter audio URLs for a LibriVox book."""
    params = {"id": book_id, "format": "json", "extended": "1"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(_LIBRIVOX_API, params=params)
        resp.raise_for_status()
        books = resp.json().get("books", [])
    if not books:
        return []
    sections = books[0].get("sections") or []
    sections.sort(key=lambda s: int(s.get("section_number") or 0))
    return [s["listen_url"] for s in sections if s.get("listen_url")]


async def _librivox_download(track: TrackInfo, dl_dir: Path) -> str | None:
    """Download every chapter and concatenate into one mono mp3."""
    try:
        urls = await _librivox_sections(track.source_id)
    except Exception as e:
        logger.error("LibriVox sections fetch failed: %s", e)
        return None
    if not urls:
        return None

    uid = uuid.uuid4().hex
    parts_dir = dl_dir / f"lv_{uid}"
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_files: list[Path] = []
    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            for i, url in enumerate(urls):
                dest = parts_dir / f"{i:04d}.mp3"
                try:
                    resp = await client.get(url)
                except Exception as e:
                    logger.warning("LibriVox chapter %s failed: %s", i, e)
                    continue
                if resp.status_code != 200:
                    logger.warning("LibriVox chapter %s -> HTTP %s", i, resp.status_code)
                    continue
                await asyncio.to_thread(dest.write_bytes, resp.content)
                if dest.stat().st_size > 0:
                    part_files.append(dest)

        if not part_files:
            return None

        # Concatenate + normalise to mono speech bitrate so part-size math holds.
        list_file = parts_dir / "concat.txt"
        list_file.write_text(
            "\n".join(f"file '{p.as_posix()}'" for p in part_files), encoding="utf-8"
        )
        out = dl_dir / f"{uid}.mp3"
        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c:a", "libmp3lame", "-b:a", f"{settings.audiobook_bitrate_kbps}k", "-ac", "1",
            str(out),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error("LibriVox concat failed: %s", stderr.decode()[-500:])
            return None
        return str(out) if out.exists() and out.stat().st_size > 0 else None
    finally:
        # Clean up chapter parts; keep only the final concatenated file.
        for p in part_files:
            p.unlink(missing_ok=True)
        for leftover in (parts_dir / "concat.txt",):
            leftover.unlink(missing_ok=True)
        try:
            parts_dir.rmdir()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────
# Provider registry + public API
# ──────────────────────────────────────────────────────────────────────────
# Order = search priority. Add new providers (sites, torrents) here.
_SEARCH = {
    "youtube": _yt_search,
    "librivox": _librivox_search,
}
_DOWNLOAD = {
    "youtube": _yt_download,
    "librivox": _librivox_download,
}


async def search(query: str, count: int = 15) -> list[TrackInfo]:
    """Search all providers in parallel, merge and rank by relevance/length."""
    tasks = [fn(query, count) for fn in _SEARCH.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    merged: list[TrackInfo] = []
    for r in results:
        if isinstance(r, list):
            merged.extend(r)
        elif isinstance(r, Exception):
            logger.warning("Audiobook provider error: %s", r)

    return _rank(merged, query)


async def download(track: TrackInfo) -> str | None:
    """Download an audiobook to a single mp3 file. Returns path or None."""
    dl_dir = settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)
    fn = _DOWNLOAD.get(track.source)
    if not fn:
        logger.error("No audiobook downloader for source %r", track.source)
        return None
    return await fn(track, dl_dir)


# ──────────────────────────────────────────────────────────────────────────
# Delivery: keep whole when we can send it, otherwise split into parts
# ──────────────────────────────────────────────────────────────────────────
async def ffprobe_duration(path: str) -> int:
    """Duration of an audio file in seconds (0 if it can't be read)."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return int(float(stdout.decode().strip()))
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", path, e)
    return 0


async def prepare_parts(file_path: str) -> list[str]:
    """Return file paths to send, in order.

    One path when the whole book fits the current send limit (e.g. the local
    Bot API server's 2 GB), otherwise the book split into <50 MB parts via a
    fast stream copy. The caller is responsible for cleaning up the returned
    paths.
    """
    size = os.path.getsize(file_path)
    if size <= settings.send_limit_bytes:
        return [file_path]

    part_bytes = settings.audiobook_part_size_mb * 1024 * 1024
    duration = await ffprobe_duration(file_path)
    if duration > 0:
        # Derive segment length from the real average bitrate (robust to the
        # actual encode), so each part lands near the target size.
        seg = max(60, int(part_bytes * duration / size))
    else:
        seg = max(60, int(part_bytes * 8 / (settings.audiobook_bitrate_kbps * 1000)))

    base = file_path[:-4] if file_path.endswith(".mp3") else file_path
    out_tmpl = f"{base}_part_%03d.mp3"
    cmd = [
        "ffmpeg", "-y", "-i", file_path,
        "-f", "segment", "-segment_time", str(seg),
        "-c", "copy", "-reset_timestamps", "1",
        out_tmpl,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.error("Audiobook split failed: %s", stderr.decode()[-500:])
        return [file_path]

    parts = sorted(glob(f"{base}_part_*.mp3"))
    return parts or [file_path]
