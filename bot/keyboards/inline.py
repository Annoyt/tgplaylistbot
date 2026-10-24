"""Inline keyboard builders for MusicBot."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from app.db.models import TrackInfo

# Reply-keyboard labels (also matched as text in the audiobook handler).
BTN_MUSIC = "🎵 Музыка"
BTN_AUDIOBOOKS = "📚 Аудиокниги"


def main_reply_kb() -> ReplyKeyboardMarkup:
    """Persistent menu: switch between music and audiobook search modes."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_MUSIC), KeyboardButton(text=BTN_AUDIOBOOKS)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Название трека или книги…",
    )



_CALLBACK_LIMIT = 64  # Telegram's callback_data limit, in bytes


def _fit_callback(prefix: str, value: str) -> str:
    """``prefix + value``, with ``value`` cut to fit 64 bytes (marked by "..").

    The limit is in UTF-8 bytes: a 37-character cut let a Cyrillic or emoji
    artist name overflow it, and Telegram then rejected the whole results message.
    """
    room = _CALLBACK_LIMIT - len(prefix.encode())
    raw = value.encode()
    if len(raw) <= room:
        return prefix + value
    return prefix + raw[:room - 2].decode("utf-8", "ignore") + ".."


def search_results_kb(
    tracks: list[TrackInfo],
    page: int,
    total: int,
    per_page: int = 10,
    sort_by: str = "",
    lossless_only: bool = False,
    artist_name: str | None = None,
) -> InlineKeyboardMarkup:
    """Build keyboard for search results: pagination + filters + playlist emojis."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    buttons: list[list[InlineKeyboardButton]] = []

    start = (page - 1) * per_page
    end = min(start + per_page, total)

    # Render track select buttons in rows of 5
    track_row1 = []
    track_row2 = []

    for i in range(start, end):
        display_idx = i + 1
        btn = InlineKeyboardButton(text=f"[{display_idx}] Выбрать", callback_data=f"select_track:{i}:{page}")
        if len(track_row1) < 5:
            track_row1.append(btn)
        else:
            track_row2.append(btn)

    if track_row1: buttons.append(track_row1)
    if track_row2: buttons.append(track_row2)

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
    ])

    if artist_name:
        buttons.append([
            InlineKeyboardButton(text="🌟 Популярные треки", callback_data=_fit_callback("popular_artist:", artist_name))
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def track_detail_kb(track_index: int, source_name: str = "MP3", is_admin: bool = False, is_cached: bool = False, page: int = 1) -> InlineKeyboardMarkup:
    """Keyboard for a specific track: only downloads."""
    buttons = []

    if is_cached:
        buttons.append([
            InlineKeyboardButton(text="⚡ Скачать быстро (из кэша)", callback_data=f"dl:{track_index}:mp3_320"),
        ])
        buttons.append([
            InlineKeyboardButton(text="🔄 Перекачать заново", callback_data=f"dl:{track_index}:mp3_320:1")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text=f"📥 Скачать из {source_name}", callback_data=f"dl:{track_index}:mp3_320"),
        ])

    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"back_to_list:{page}")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)



def link_menu_kb(token: str) -> InlineKeyboardMarkup:
    """What to do with a pasted link: the video, its audio, or find its track."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎬 Скачать видео", callback_data=f"lv:{token}"),
            InlineKeyboardButton(text="🎧 Звук MP3", callback_data=f"lva:{token}"),
        ],
        [InlineKeyboardButton(text="🎵 Найти трек из видео", callback_data=f"lr:{token}")],
    ])


def _mb_label(base: str, size_bytes: int) -> str:
    """Append an approx size like '720p (~18MB)' when known.

    Small files get KB / one decimal: whole MB showed a 220KB Short as "~0MB".
    """
    if not size_bytes or size_bytes <= 0:
        return base
    mb = size_bytes / (1024 * 1024)
    if mb < 1:
        size = f"{max(1, round(size_bytes / 1024))}KB"
    elif mb < 10:
        size = f"{mb:.1f}MB"
    else:
        size = f"{mb:.0f}MB"
    return f"{base} (~{size})"


def video_res_kb(token: str, sizes: dict | None = None) -> InlineKeyboardMarkup:
    """Resolution picker + audio-only / send-as-file options.

    ``sizes`` (from youtube.probe_video_sizes) annotates buttons with ~MB.
    """
    sizes = sizes or {}
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=_mb_label("🏆 Лучшее", sizes.get("best", 0)), callback_data=f"lvr:{token}:best"),
            InlineKeyboardButton(text=_mb_label("720p", sizes.get("720", 0)), callback_data=f"lvr:{token}:720"),
            InlineKeyboardButton(text=_mb_label("360p", sizes.get("360", 0)), callback_data=f"lvr:{token}:360"),
        ],
        [
            InlineKeyboardButton(text="🎵 Только звук (MP3)", callback_data=f"lva:{token}"),
            InlineKeyboardButton(text="📄 Как файл", callback_data=f"lvf:{token}"),
        ],
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


def confirm_topic_kb(emoji: str, track_idx: int = 0) -> InlineKeyboardMarkup:
    """Confirm creating a new forum topic."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"topic:create:{emoji}:{track_idx}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="topic:cancel"),
        ],
    ])


# ── Audiobooks ────────────────────────────────────────────────────────────
def audiobook_results_kb(
    total: int, page: int, per_page: int = 6
) -> InlineKeyboardMarkup:
    """Select buttons + pagination for audiobook search results."""
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    buttons: list[list[InlineKeyboardButton]] = []

    for i in range(start, end):
        buttons.append([
            InlineKeyboardButton(text=f"📖 [{i + 1}] Выбрать", callback_data=f"bsel:{i}:{page}")
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bpage:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="bpage:noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bpage:{page + 1}"))
    buttons.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def audiobook_detail_kb(book_index: int, source_name: str = "", page: int = 1) -> InlineKeyboardMarkup:
    """Keyboard for one audiobook: download + back."""
    label = f"📥 Скачать книгу из {source_name}" if source_name else "📥 Скачать книгу"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"bdl:{book_index}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"bback:{page}")],
    ])
