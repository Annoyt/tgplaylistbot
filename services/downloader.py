"""Unified download manager: routes to the right service, handles cleanup."""

from __future__ import annotations

import logging
import os
import asyncio
import time

from app.db.models import TrackInfo
from config import settings
from services import youtube as yt_service
from services import vk_music as vk_service
from services.vk_captcha import captcha_manager
from bot.handlers.admin import get_global_settings

logger = logging.getLogger(__name__)

# Global lock and timestamp to enforce download delay
_download_lock = asyncio.Lock()
_last_download_time = 0.0

async def download_track(
    track: TrackInfo,
    quality: str = "mp3_320",
    bot=None,
    chat_id: int = 0,
    user_id: int = 0,
) -> str | None:
    """Download a track from its source. Returns file path or None. Enforces global delay."""
    global _last_download_time

    # Enforce delay but don't hold lock during download
    async with _download_lock:
        g_settings = await get_global_settings()
        delay = g_settings.download_delay_sec
        now = time.time()
        elapsed = now - _last_download_time
        if elapsed < delay:
            wait_time = delay - elapsed
            logger.info(f"Enforcing download delay, sleeping for {wait_time:.1f}s")
            await asyncio.sleep(wait_time)

        _last_download_time = time.time()

    # Lock released, now download
    if track.source == "youtube":
        return await yt_service.download(track, quality)
    elif track.source == "vk":
        # Create captcha handler if bot info is provided
        c_handler = None
        if bot and chat_id and user_id:
            c_handler = captcha_manager.get_captcha_handler(bot, chat_id, user_id)
            
        # Add to my audios as an anti-bot measure before downloading
        await vk_service.add_track_to_my_audios(track, captcha_handler=c_handler)
        return await vk_service.download(track, quality, captcha_handler=c_handler)
    elif track.source == "spotify":
        query = f"{track.artist} {track.title}"
        yt_results = await yt_service.search(query, count=1)
        if yt_results:
            return await yt_service.download(yt_results[0], quality)
        return None
    else:
        logger.warning(f"Unknown source: {track.source}")
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
            except Exception:
                pass
