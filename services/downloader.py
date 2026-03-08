"""Unified download manager: routes to the right service, handles cleanup."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.db.models import TrackInfo
from config import settings
from services import youtube as yt_service
from services import vk_music as vk_service

logger = logging.getLogger(__name__)


async def download_track(
    track: TrackInfo,
    quality: str = "mp3_320",
) -> str | None:
    """Download a track from its source. Returns file path or None."""
    if track.source == "youtube":
        return await yt_service.download(track, quality)
    elif track.source == "vk":
        return await vk_service.download(track, quality)
    elif track.source == "spotify":
        # Spotify doesn't allow direct download → search on YouTube and download
        query = f"{track.artist} {track.title}"
        yt_results = await yt_service.search(query, count=1)
        if yt_results:
            return await yt_service.download(yt_results[0], quality)
        return None
    else:
        logger.error("Unknown source: %s", track.source)
        return None


def cleanup_file(file_path: str) -> None:
    """Remove a temporary file after sending."""
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.debug("Cleaned up: %s", file_path)
    except OSError as e:
        logger.warning("Cleanup failed for %s: %s", file_path, e)


def cleanup_download_dir() -> None:
    """Remove all files in the download directory."""
    dl = settings.download_path
    if not dl.exists():
        return
    for f in dl.iterdir():
        if f.is_file():
            try:
                f.unlink()
            except OSError:
                pass
