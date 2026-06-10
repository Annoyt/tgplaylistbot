"""VK Music search and download via vk_api."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path


from app.db.models import TrackInfo
from config import settings
from services.youtube import BROWSER_UA

logger = logging.getLogger(__name__)

async def _get_vk_creds():
    """Get VK credentials from bot_settings table."""
    from app.db.database import get_db
    creds = {"login": settings.vk_login, "password": settings.vk_password, "token": settings.vk_token}
    try:
        db = await get_db()
        # Ensure we are using the correct table schema (key-value based)
        rows = await db.execute("SELECT key, value FROM bot_settings WHERE key IN ('vk_login', 'vk_password', 'vk_token')")
        for row in await rows.fetchall():
            if row["value"]:
                k = row["key"].replace('vk_', '')
                creds[k] = row["value"]
    except Exception as e:
        logger.error(f"Failed to fetch VK creds from DB: {e}")
    return creds

OFFICIAL_UA = "VKAndroidApp/8.53-15779 (Android 13; SDK 33; arm64-v8a; Xiaomi Redmi Note 11; ru; 2400x1080)"

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
            # Use Kate Mobile app_id (2274003) for better music access
            session = vk_api.VkApi(
                login=login,
                password=password,
                app_id=2274003,
                captcha_handler=captcha_handler
            )
            try:
                session.auth(token_only=True)
                logger.info("VK Audio: Auth SUCCESSFUL.")
            except Exception as auth_err:
                logger.error(f"VK Audio: Auth FAILED: {auth_err}")
                raise

        # Inject official User-Agent into the session
        session.http.headers.update({"User-Agent": OFFICIAL_UA})

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

        def _direct_search():
            # Use direct API method instead of VkAudio.search which is broken
            # Added sort=2 (by popularity) and autocomplete=1
            res = vk_audio._vk.method("audio.search", {
                "q": query, 
                "count": count,
                "sort": 2,
                "autocomplete": 1
            })
            return res.get("items", [])

        results = await loop.run_in_executor(None, _direct_search)

        tracks: list[TrackInfo] = []
        for item in results:
            tracks.append(
                TrackInfo(
                    title=item.get("title", "Unknown"),
                    artist=item.get("artist", "Unknown"),
                    duration=int(item.get("duration", 0)),
                    source="vk",
                    source_id=f"https://vk.com/audio{item.get('owner_id')}_{item.get('id')}",
                    bitrate=320 if item.get("is_hq") else 0,  # 0 allows models.py to fallback to ~320k
                    filesize=0,
                )
            )
        logger.info(f"VK search for '{query}' found {len(tracks)} tracks.")
        return tracks
    except Exception as e:
        logger.error("VK search failed: %s", e, exc_info=True)
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
            # Use direct API method instead of VkAudio.get_iter which can also be fragile
            params = {
                "owner_id": int(owner_id),
                "count": 200,
            }
            if playlist_id and int(playlist_id) != 0:
                params["album_id"] = int(playlist_id)
            if access_key:
                params["access_key"] = access_key
            
            res = vk_audio._vk.method("audio.get", params)
            return res.get("items", [])

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
    """
    Download audio from VK.

    VK serves audio as HLS streams (m3u8). This function:
    1. Resolves the real audio URL via audio.getById
    2. Detects if it's HLS or direct
    3. Downloads using the appropriate method
    4. Returns path to the final MP3 file
    """
    if not track.source_id:
        return None

    from services.hls_downloader import (
        detect_url_type,
        download_direct_streaming,
        download_hls,
    )

    dl_url = track.source_id
    creds = await _get_vk_creds()

    # Step 1: Resolve the actual audio URL from VK API
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
                        try:
                            res = vk_audio._vk.method("audio.getById", {
                                "audios": f"{owner_id}_{audio_id}"
                            })
                            return res[0].get("url") if res else None
                        except Exception as e:
                            logger.error(f"VK audio.getById failed: {e}")
                            return None
                    resolved_url = await loop.run_in_executor(None, _get_url)
                    if resolved_url:
                        dl_url = resolved_url
                        logger.info(f"VK URL resolved: {dl_url[:120]}...")
                    else:
                        logger.error("VK audio.getById returned no URL")
                        return None
            except Exception as e:
                logger.error(f"Failed to resolve VK audio URL: {e}")
                return None

    if not dl_url or dl_url == track.source_id:
        logger.error("Could not resolve a download URL for VK track")
        return None

    dl_dir = download_dir or settings.download_path
    dl_dir.mkdir(parents=True, exist_ok=True)

    import uuid as _uuid
    uid = _uuid.uuid4().hex
    file_path = dl_dir / f"{uid}.mp3"

    # Common headers for VK requests
    vk_headers = {
        "User-Agent": OFFICIAL_UA,
        "Referer": "https://vk.com/",
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    # Step 2: Detect URL type
    url_type = await detect_url_type(dl_url, headers=vk_headers)
    logger.info(f"VK URL type detected: {url_type}")

    # Step 3: Download based on type
    if url_type == "hls":
        logger.info(f"VK: Downloading HLS stream for '{track.artist} - {track.title}'...")
        ok = await download_hls(
            url=dl_url,
            output_path=file_path,
            headers=vk_headers,
            expected_duration=track.duration,
        )
        if ok and file_path.exists():
            # Final size check
            if file_path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
                logger.warning(f"VK HLS track too large: {file_path.stat().st_size} bytes")
                file_path.unlink(missing_ok=True)
                return None
            return str(file_path)
        logger.warning("VK HLS download failed, trying yt-dlp as fallback...")
        return await _ytdlp_fallback(dl_url, dl_dir, uid)

    elif url_type == "direct":
        logger.info(f"VK: Direct download for '{track.artist} - {track.title}'...")
        ok = await download_direct_streaming(
            url=dl_url,
            output_path=file_path,
            headers=vk_headers,
            max_size_bytes=settings.max_file_size_mb * 1024 * 1024,
        )
        if ok and file_path.exists():
            return str(file_path)
        logger.warning("VK direct download failed")
        return None

    else:
        # Unknown type — try HLS first, then direct, then yt-dlp
        logger.info(f"VK: Unknown URL type, trying HLS first...")
        ok = await download_hls(
            url=dl_url,
            output_path=file_path,
            headers=vk_headers,
            expected_duration=track.duration,
        )
        if ok and file_path.exists():
            if file_path.stat().st_size > settings.max_file_size_mb * 1024 * 1024:
                file_path.unlink(missing_ok=True)
                return None
            return str(file_path)

        logger.info("VK: HLS failed, trying direct download...")
        ok = await download_direct_streaming(
            url=dl_url,
            output_path=file_path,
            headers=vk_headers,
            max_size_bytes=settings.max_file_size_mb * 1024 * 1024,
        )
        if ok and file_path.exists():
            return str(file_path)

        logger.info("VK: Direct failed, trying yt-dlp fallback...")
        return await _ytdlp_fallback(dl_url, dl_dir, uid)


async def _ytdlp_fallback(url: str, dl_dir: Path, uid: str) -> str | None:
    """Last-resort download via yt-dlp with corrected flags."""
    output_template = str(dl_dir / f"{uid}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--user-agent", BROWSER_UA,
        "--extractor-args", "youtube:player_client=web",
        "-4",
        "--hls-prefer-native",
        "--fragment-retries", "10",
        "--retries", "3",
        "--no-part",
        "--fixup", "detect_or_warn",
        "-o", output_template,
        url,
    ]
    for arg in settings.cookie_args():
        cmd.insert(-1, arg)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error(f"yt-dlp VK fallback failed: {stderr.decode()[-500:]}")
        return None

    out_file = dl_dir / f"{uid}.mp3"
    if out_file.exists() and out_file.stat().st_size > 0:
        return str(out_file)
    return None
