"""Inline keyboard builders for MusicBot."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.db.models import TrackInfo


def search_results_kb(
    tracks: list[TrackInfo],
    page: int,
    total: int,
    per_page: int = 10,
    sort_by: str = "",
    lossless_only: bool = False,
) -> InlineKeyboardMarkup:
    """Build keyboard for search results: pagination + filters + playlist emojis."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    buttons: list[list[InlineKeyboardButton]] = []

    # Row 1-2: Pagination
    nav_row1: list[InlineKeyboardButton] = []
    nav_row2: list[InlineKeyboardButton] = []

    if page > 1:
        nav_row1.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{page-1}:{sort_by}:{int(lossless_only)}"))

    for p in range(1, min(total_pages + 1, 6)):
        text = f"·{p}·" if p == page else str(p)
        nav_row1.append(InlineKeyboardButton(text=text, callback_data=f"page:{p}:{sort_by}:{int(lossless_only)}"))

    if total_pages > 5 and page < total_pages:
        nav_row1.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{page+1}:{sort_by}:{int(lossless_only)}"))

    buttons.append(nav_row1)

    if total_pages > 5:
        for p in range(6, min(total_pages + 1, 11)):
            text = f"·{p}·" if p == page else str(p)
            nav_row2.append(InlineKeyboardButton(text=text, callback_data=f"page:{p}:{sort_by}:{int(lossless_only)}"))
        if nav_row2:
            buttons.append(nav_row2)

    # Row 3: Filters
    br_text = "BR: ↑" if sort_by != "bitrate_desc" else "BR: ↓"
    br_val = "bitrate_desc" if sort_by != "bitrate_desc" else "bitrate_asc"
    lossless_text = "Lossless: ✅" if lossless_only else "Lossless: ❌"

    buttons.append([
        InlineKeyboardButton(text=br_text, callback_data=f"filter:br:{br_val}:{page}"),
        InlineKeyboardButton(text=lossless_text, callback_data=f"filter:lossless:{int(not lossless_only)}:{page}"),
        InlineKeyboardButton(text="Title", callback_data=f"filter:title:asc:{page}"),
    ])

    # Row 4: Playlist emojis
    buttons.append([
        InlineKeyboardButton(text="❤️", callback_data="playlist:❤️"),
        InlineKeyboardButton(text="🔥", callback_data="playlist:🔥"),
        InlineKeyboardButton(text="😢", callback_data="playlist:😢"),
        InlineKeyboardButton(text="🎉", callback_data="playlist:🎉"),
        InlineKeyboardButton(text="📁+", callback_data="playlist:new"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def track_detail_kb(track_index: int) -> InlineKeyboardMarkup:
    """Keyboard for a specific track: download buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⬇️ MP3 320", callback_data=f"dl:{track_index}:mp3_320"),
            InlineKeyboardButton(text="⬇️ FLAC", callback_data=f"dl:{track_index}:flac"),
        ],
        [InlineKeyboardButton(text="← Назад к списку", callback_data="back_to_list")],
    ])


def settings_kb(quality: str, priority: str) -> InlineKeyboardMarkup:
    """Settings menu keyboard."""
    q_mp3 = "✅ " if quality == "mp3_320" else ""
    q_flac = "✅ " if quality == "flac" else ""

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎵 Качество", callback_data="set:header")],
        [
            InlineKeyboardButton(text=f"{q_mp3}MP3 320kbps", callback_data="set:quality:mp3_320"),
            InlineKeyboardButton(text=f"{q_flac}FLAC", callback_data="set:quality:flac"),
        ],
        [InlineKeyboardButton(text="🔄 Приоритет платформ", callback_data="set:header2")],
        [InlineKeyboardButton(text=f"📋 {priority}", callback_data="set:priority:cycle")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="set:close")],
    ])


def confirm_topic_kb(emoji: str) -> InlineKeyboardMarkup:
    """Confirm creating a new forum topic."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"topic:create:{emoji}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="topic:cancel"),
        ],
    ])
