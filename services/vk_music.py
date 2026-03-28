"""VK Music search and download via vk_api."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)

async def _get_vk_creds():
    """Get VK credentials from settings table."""
    from app.db.database import get_db
    creds = {"login": settings.vk_login, "password": settings.vk_password, "token": settings.vk_token}
    try:
        db = await get_db()
        # Ensure vk_token column exists (auto-migration)
        try:
            await db.execute("ALTER TABLE settings ADD COLUMN vk_token TEXT")
            await db.commit()
        except: pass

        row = await db.execute("SELECT * FROM settings LIMIT 1")
        res = await row.fetchone()
        if res:
            if res.get("vk_login"): creds["login"] = res["vk_login"]
            if res.get("vk_password"): creds["password"] = res["vk_password"]
            if res.get("vk_token"): creds["token"] = res["vk_token"]
    except Exception as e:
        logger.error(f"Failed to fetch VK creds from DB: {e}")
    return creds

def _init_vk_audio(creds: dict, captcha_handler=None):
    """Initialize VK audio session (blocking, run in executor)."""
    import vk_api
    from vk_api.audio import VkAudio

    try:
        login = creds.get("login")
        password = creds.get("password")
        token = creds.get("token")

        if not token and not (login and password):
            logger.warning("VK Audio: No credentials found in settings.")
            return None

        # Prioritize token over login/password
        if token:
            logger.info("VK Audio: Initializing session via TOKEN...")
            session = vk_api.VkApi(token=token, captcha_handler=captcha_handler)
        elif login and password:
            logger.info(f"VK Audio: Initializing session via LOGIN ({login[:3]}***)...")
            # Use Kate Mobile app_id for better music access
            session = vk_api.VkApi(
                login=login,
                password=password,
                app_id=2685278,
                client_secret="lYp6pS1pgaY9w6raRrEP",
                captcha_handler=captcha_handler
            )
            try:
                session.auth(token_only=True)
                logger.info("VK Audio: Auth SUCCESSFUL.")
            except Exception as auth_err:
                logger.error(f"VK Audio: Auth FAILED: {auth_err}")
                raise

        vk_audio = VkAudio(session)
        logger.info("VK Audio: VkAudio object created successfully.")
        return vk_audio

    except Exception as e:
        logger.error(f"VK Audio: CRITICAL INIT ERROR: {e}", exc_info=True)
        return None

async def search(query: str, count: int = 30, captcha_handler=None) -> list[TrackInfo]:
    """Search VK for audio tracks."""
    creds = await _get_vk_creds()
    if not creds.get("token") and not (creds.get("login") and creds.get("password")):
        return []

    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds, captcha_handler)
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

async def add_track_to_my_audios(track: TrackInfo, captcha_handler=None) -> bool:
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
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds, captcha_handler)
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

async def get_playlist_tracks(owner_id: str, playlist_id: str, access_key: str = "", captcha_handler=None) -> list[TrackInfo]:
    """Fetch tracks from a VK playlist."""
    creds = await _get_vk_creds()
    loop = asyncio.get_event_loop()
    try:
        vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds, captcha_handler)
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
    captcha_handler=None,
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
                vk_audio = await loop.run_in_executor(None, _init_vk_audio, creds, captcha_handler)
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
