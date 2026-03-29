"""Text search handler: unified search across YouTube, VK, Spotify."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from aiogram import F, Router
from aiogram.types import Message

from app.db.models import TrackInfo
from bot.keyboards.inline import search_results_kb
from services import spotify as sp_svc
from services import vk_music as vk_svc
from services import youtube as yt_svc
from services.vk_captcha import captcha_manager

router = Router()
logger = logging.getLogger(__name__)

# In-memory cache for search results per user (user_id -> list[TrackInfo])
_MAX_CACHE = 500
_search_cache: OrderedDict[int, list[TrackInfo]] = OrderedDict()


def _format_results(tracks: list[TrackInfo], page: int, per_page: int, total: int) -> str:
    """Format search results as numbered list."""
    start = (page - 1) * per_page
    lines = [f"🔍 Результаты {start+1}-{min(start+per_page, total)} из {total}\n"]

    for i, t in enumerate(tracks[start:start+per_page], start=start+1):
        lines.append(
            f"{i}. {t.artist} – {t.title} "
            f"{t.duration_str} {t.source_icon} "
            f"{t.size_str} {t.bitrate_str}"
        )

    return "\n".join(lines)


@router.message(F.text & ~F.text.startswith("/") & ~F.text.startswith("http"))
async def text_search(message: Message) -> None:
    """Handle text search queries."""


    query = message.text.strip()
    if not query or len(query) < 2:
        return

    # Cross-Topic Routing: If requested in a specific topic, move it to General
    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    # Always send status to the General topic or DM
    status_msg = await message.bot.send_message(
        chat_id=message.chat.id,
        text=f"🔍 Ищу на всех платформах: <b>{query}</b>...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None),
        parse_mode="HTML"
    )

    try:
        # Parallel search
        c_handler = captcha_manager.get_captcha_handler(
            message.bot, 
            message.chat.id, 
            message.from_user.id
        )
        yt_task = yt_svc.search(query, count=30)
        vk_task = vk_svc.search(query, count=30, captcha_handler=c_handler)
        sp_task = sp_svc.search(query, count=30)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)

        yt_res = results[0] if isinstance(results[0], list) else []
        vk_res = results[1] if isinstance(results[1], list) else []
        sp_res = results[2] if isinstance(results[2], list) else []

        platform_res = {
            "youtube": yt_res,
            "vk": vk_res,
            "spotify": sp_res
        }

        from app.db.database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT platform_priority FROM user_settings WHERE user_id = ?", (message.from_user.id,))
            data = await row.fetchone()
            platforms = [p.strip() for p in data["platform_priority"].split(",")] if data else ["youtube", "vk", "spotify"]
        finally:
            await db.close()

        all_tracks: list[TrackInfo] = []
        for p in platforms:
            all_tracks.extend(platform_res.get(p, []))

        for r in results:
            if isinstance(r, Exception):
                logger.warning("Search error: %s", r)

        if not all_tracks:
            await status_msg.edit_text("😔 Ничего не найдено")
            return

        # Sort and Cache results (evict oldest if over limit)
        all_tracks = sort_tracks(all_tracks, "")
        user_id = message.from_user.id
        _search_cache[user_id] = all_tracks
        if len(_search_cache) > _MAX_CACHE:
            _search_cache.popitem(last=False)
        per_page = 10
        artist_name = all_tracks[0].artist if all_tracks else None
        text = _format_results(all_tracks, page=1, per_page=per_page, total=len(all_tracks))
        kb = search_results_kb(all_tracks, page=1, total=len(all_tracks), per_page=per_page, artist_name=artist_name)

        await status_msg.edit_text(text, reply_markup=kb)

    except Exception as e:
        logger.error("Search failed: %s", e)
        await status_msg.edit_text("❌ Произошла ошибка при поиске. Попробуйте позже.")


def get_cached_tracks(user_id: int) -> list[TrackInfo]:
    """Get cached search results for a user."""
    return _search_cache.get(user_id, [])


def sort_tracks(tracks: list[TrackInfo], sort_by: str) -> list[TrackInfo]:
    """Sort tracks by given criteria."""
    if sort_by == "bitrate_desc":
        return sorted(tracks, key=lambda t: t.bitrate, reverse=True)
    elif sort_by == "bitrate_asc":
        return sorted(tracks, key=lambda t: t.bitrate)
    elif sort_by in ("title", "asc"):
        return sorted(tracks, key=lambda t: t.title.lower())
    
    def default_sort_key(t: TrackInfo):
        # Primary: Duration (ascending)
        # Secondary: Estimated or real size (descending)
        # We calculate an effective size in MB for sorting
        if t.filesize > 0:
            eff_size = t.filesize
        else:
            # Estimate: YouTube tracks are usually around 128-160kbps
            # 160 kbps = 20 KB/s
            eff_size = t.duration * 20 * 1024
            
        return (t.duration, -eff_size)

    return sorted(tracks, key=default_sort_key)


def filter_lossless(tracks: list[TrackInfo]) -> list[TrackInfo]:
    """Filter to lossless tracks only."""
    return [t for t in tracks if t.is_lossless or t.bitrate >= 320]




@router.message(F.text.regexp(r'https?://(?:m\.)?vk\.com/(?:music/playlist/|audio\?z=audio_playlist)(-?\d+)_(\d+)(?:(?:/|%2F|_)([a-zA-Z0-9]+))?'))
async def vk_playlist_url(message: Message) -> None:
    """Handle VK playlist URLs."""
    text = message.text.strip()

    # Extract owner_id, playlist_id, and access_key (if present)
    import re
    match = re.search(r'(?:music/playlist/|audio\?z=audio_playlist)(-?\d+)_(\d+)(?:(?:/|%2F|_)([a-zA-Z0-9]+))?', text)
    if not match:
        return

    owner_id, playlist_id, access_key = match.groups()
    access_key = access_key or ""

    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    status_msg = await message.bot.send_message(
        chat_id=message.chat.id,
        text="⏳ Получаю список треков из плейлиста ВК...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)
    )

    try:
        from services import vk_music as vk_svc
        tracks = await vk_svc.get_playlist_tracks(owner_id, playlist_id, access_key)

        if not tracks:
            await status_msg.edit_text("❌ Не удалось получить треки. Плейлист пуст, закрыт или недоступен.")
            return

        # Add tracks to playlist_queue
        import json
        from dataclasses import asdict

        from app.db.database import get_db

        db = await get_db()
        try:
            user_id = message.from_user.id
            orig_id = message.message_id

            # Save session for original_msg_id if needed, but we can just use message.message_id

            inserted = 0
            for t in tracks:
                await db.execute(
                    "INSERT INTO playlist_queue (chat_id, user_id, original_msg_id, track_json) VALUES (?, ?, ?, ?)",
                    (message.chat.id, user_id, orig_id, json.dumps(asdict(t)))
                )
                inserted += 1

            await db.commit()
            await status_msg.edit_text(f"✅ Найдено {inserted} треков в плейлисте. Добавлено в фоновую очередь загрузки.\nБот будет скачивать их постепенно (с паузами), чтобы избежать блокировки.")
        except Exception as e:
            logger.error(f"Failed to save playlist queue: {e}")
            await status_msg.edit_text("❌ Ошибка при добавлении в очередь.")
        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Playlist extraction failed: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при получении плейлиста.")
