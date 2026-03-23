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

async def _get_vk_creds():
    """Get VK credentials from database or fallback to .env"""
    from app.db.database import get_db_ctx
    creds = {"login": settings.vk_login, "password": settings.vk_password, "token": settings.vk_token}
    try:
        async with get_db_ctx() as db:
            rows = await db.execute("SELECT key, value FROM bot_settings WHERE key IN ('vk_login', 'vk_password', 'vk_token')")
            for row in await rows.fetchall():
                if row["value"]:
                    creds[row["key"].replace('vk_', '')] = row["value"]
    except Exception as e:
        logger.error(f"Failed to fetch VK creds from DB: {e}")
    return creds

def _init_vk_audio(creds: dict):
    """Initialize VK audio session (blocking, run in executor)."""
    import vk_api

    try:
        login = creds.get("login")
        password = creds.get("password")
        token = creds.get("token")

        if login and password:
            logger.info("Initializing VK session via login/password...")
            session = vk_api.VkApi(login=login, password=password)
            session.auth(token_only=True)
        elif token:
            logger.info("Initializing VK session via token...")
            session = vk_api.VkApi(token=token)
        else:
            logger.warning("No VK credentials provided.")
            return None

        from vk_api.audio import VkAudio
        return VkAudio(session)
    except Exception as e:
        logger.error("VK Audio init failed: %s", e)
        return None

async def search(query: str, count: int = 30) -> list[TrackInfo]:
    """Search VK for audio tracks."""
    creds = await _get_vk_creds()
    if not creds.get("token") and not (creds.get("login") and creds.get("password")):
        return []

    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds)
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
                    source_id=f"https://vk.com/audio{item.get('owner_id')}_{item.get('id')}",
                    bitrate=320 if item.get("is_hq") else 128,
                    filesize=0,
                )
            )
        return tracks
    except Exception as e:
        logger.error("VK search failed: %s", e)
        return []

async def add_track_to_my_audios(track: TrackInfo) -> bool:
    """Add track to VK audios to mimic human behavior."""
    if not track.source_id or "audio" not in track.source_id:
        return False

    import re
    match = re.search(r"audio(-?\d+)_(\d+)", track.source_id)
    if not match:
        logger.warning(f"Could not extract audio ID from {track.source_id}. Skipping 'add_to_my_audios'.")
        return False

    owner_id, audio_id = match.groups()

    creds = await _get_vk_creds()
    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds)
        if vk_audio is None:
            return False

        def _add():
            vk_audio._vk.method("audio.add", {
                "audio_id": audio_id,
                "owner_id": owner_id
            })

        await loop.run_in_executor(None, _add)
        logger.info(f"Successfully added track {track.artist} - {track.title} to VK audios.")
        return True
    except Exception as e:
        logger.error(f"Failed to add track to VK audios: {e}")
        return False

async def get_playlist_tracks(owner_id: str, playlist_id: str, access_key: str = "") -> list[TrackInfo]:
    """Fetch tracks from a VK playlist."""
    creds = await _get_vk_creds()
    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds)
        if vk_audio is None:
            return []

        def _get_tracks():
            return list(vk_audio.get_iter(owner_id=int(owner_id), album_id=int(playlist_id), access_hash=access_key))

        results = await loop.run_in_executor(None, _get_tracks)

        tracks: list[TrackInfo] = []
        for item in results:
            tracks.append(
                TrackInfo(
                    title=item.get("title", "Unknown"),
                    artist=item.get("artist", "Unknown"),
                    duration=int(item.get("duration", 0)),
                    source="vk",
                    source_id=f"https://vk.com/audio{item.get('owner_id')}_{item.get('id')}",
                    bitrate=320 if item.get("is_hq") else 128,
                    filesize=0,
                )
            )
        return tracks
    except Exception as e:
        logger.error(f"VK get_playlist_tracks failed: {e}")
        return []

async def download(
    track: TrackInfo,
    quality: str = "mp3_320",
    download_dir: Path | None = None,
) -> str | None:
    """Download audio from VK direct URL."""
    if not track.source_id:
        return None

    dl_url = track.source_id
    creds = await _get_vk_creds()

    if "vk.com/audio" in dl_url and "mp3" not in dl_url:
        import re
        match = re.search(r"audio(-?\d+)_(\d+)", dl_url)
        if match:
            owner_id, audio_id = match.groups()
            loop = asyncio.get_event_loop()
            try:
                vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds)
                if vk_audio:
                    def _get_url():
                        res = vk_audio.get_audio_by_id(owner_id, audio_id)
                        return list(res)[0].get("url") if res else None
                    resolved_url = await loop.run_in_executor(None, _get_url)
                    if resolved_url:
                        dl_url = resolved_url
            except Exception as e:
                logger.error(f"Failed to resolve VK audio URL: {e}")
                return None

    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    import uuid
    uid = uuid.uuid4().hex
    file_path = dl_dir / f"{uid}.mp3"

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(dl_url)
            if r.status_code != 200:
                return None

            if len(r.content) > settings.max_file_size_mb * 1024 * 1024:
                logger.warning("VK track too large: %d bytes", len(r.content))
                return None

            file_path.write_bytes(r.content)
            return str(file_path)
    except Exception as e:
        logger.error(f"VK download failed: {e}")
        return None
