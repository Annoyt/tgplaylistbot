"""VK Music search and download via vk_api."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)


def _init_vk_audio():
    """Initialize VK audio session (blocking, run in executor)."""
    import vk_api

    session = vk_api.VkApi(token=settings.vk_token)
    try:
        from vk_api.audio import VkAudio
        return VkAudio(session)
    except Exception as e:
        logger.error("VK Audio init failed: %s", e)
        return None


async def search(query: str, count: int = 30) -> list[TrackInfo]:
    """Search VK for audio tracks."""
    if not settings.vk_token:
        return []

    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio)
        if vk_audio is None:
            return []

        results = await loop.run_in_executor(
            None, lambda: list(vk_audio.search(q=query, count=count))
        )

        tracks: list[TrackInfo] = []
        for item in results:
            tracks.append(
                TrackInfo(
                    title=item.get("title", "Unknown"),
                    artist=item.get("artist", "Unknown"),
                    duration=int(item.get("duration", 0)),
                    source="vk",
                    source_id=item.get("url", ""),
                    bitrate=320 if item.get("is_hq") else 128,
                    filesize=0,
                )
            )
        return tracks
    except Exception as e:
        logger.error("VK search failed: %s", e)
        return []


async def download(
    track: TrackInfo,
    quality: str = "mp3_320",
    download_dir: Path | None = None,
) -> str | None:
    """Download audio from VK direct URL."""
    if not track.source_id:
        return None

    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in f"{track.artist} - {track.title}")
    file_path = dl_dir / f"{safe_name}.mp3"

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(track.source_id)
            if r.status_code != 200:
                return None

            if len(r.content) > settings.max_file_size_mb * 1024 * 1024:
                logger.warning("VK track too large: %d bytes", len(r.content))
                return None

            file_path.write_bytes(r.content)
            return str(file_path)
    except Exception as e:
        logger.error("VK download failed: %s", e)
        return None
