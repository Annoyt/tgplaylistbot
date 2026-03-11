"""Text search handler: unified search across YouTube, VK, Spotify."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Any

from aiogram import F, Router
from aiogram.types import Message

from app.db.models import TrackInfo
from bot.keyboards.inline import search_results_kb
from services import youtube as yt_svc, vk_music as vk_svc, spotify as sp_svc

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

    status_msg = await message.answer("🔍 Ищу на всех платформах...")

    try:
        # Parallel search
        yt_task = yt_svc.search(query, count=30)
        vk_task = vk_svc.search(query, count=30)
        sp_task = sp_svc.search(query, count=30)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)

        all_tracks: list[TrackInfo] = []
        for r in results:
            if isinstance(r, list):
                all_tracks.extend(r)
            elif isinstance(r, Exception):
                logger.warning("Search error: %s", r)

        if not all_tracks:
            await status_msg.edit_text("😔 Ничего не найдено")
            return

        # Cache results (evict oldest if over limit)
        user_id = message.from_user.id
        _search_cache[user_id] = all_tracks
        if len(_search_cache) > _MAX_CACHE:
            _search_cache.popitem(last=False)

        # Display first page
        per_page = 10
        text = _format_results(all_tracks, page=1, per_page=per_page, total=len(all_tracks))
        kb = search_results_kb(all_tracks, page=1, total=len(all_tracks), per_page=per_page)

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
    return tracks


def filter_lossless(tracks: list[TrackInfo]) -> list[TrackInfo]:
    """Filter to lossless tracks only."""
    return [t for t in tracks if t.is_lossless or t.bitrate >= 320]
