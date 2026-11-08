"""Text search handler: unified search across YouTube, VK, Spotify."""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from html import escape

from aiogram import F, Router
from aiogram.types import Message

from app.db.models import TrackInfo
from bot.keyboards.inline import search_results_kb
from services.music_search import SOURCES, Cursor, SearchSession, fetch_batch
from services.ranking import Query, analyze_query, is_official, rank
from services.vk_captcha import captcha_manager

router = Router()
logger = logging.getLogger(__name__)

# Per-user search sessions (user_id -> SearchSession), oldest evicted first.
_MAX_CACHE = 500
_search_cache: OrderedDict[int, SearchSession] = OrderedDict()

PER_PAGE = 10


def store_results(
    user_id: int,
    tracks: list[TrackInfo],
    *,
    query: str = "",
    rank_query: Query | None = None,
    cursor: Cursor | None = None,
    min_score: float = 0.0,
) -> SearchSession:
    """Make ``tracks`` the user's current results. With ``query`` and
    ``cursor`` the "Ещё" button can fetch more of them."""
    session = SearchSession(
        query=query,
        rank_query=rank_query or Query(),
        tracks=list(tracks),
        cursor=cursor or Cursor(exhausted=set(SOURCES)),
        min_score=min_score,
    )
    _search_cache[user_id] = session
    _search_cache.move_to_end(user_id)
    while len(_search_cache) > _MAX_CACHE:
        _search_cache.popitem(last=False)
    return session


def get_session(user_id: int) -> SearchSession | None:
    return _search_cache.get(user_id)


def get_cached_tracks(user_id: int) -> list[TrackInfo]:
    """Get cached search results for a user."""
    session = _search_cache.get(user_id)
    return session.tracks if session else []


async def new_search(
    user_id: int,
    query: str,
    *,
    artist: str = "",
    title: str | None = None,
    artist_mode: bool = False,
    captcha_handler=None,
    sources: tuple[str, ...] = SOURCES,
    min_score: float = 0.0,
) -> SearchSession:
    """First batch for ``query``, ranked for ``artist``/``title`` (default:
    the query itself), stored as the user's session."""
    cursor = Cursor(sources=sources)
    batch = await fetch_batch(query, cursor, captcha_handler)
    rank_query = analyze_query(artist, query if title is None else title, batch, artist_mode=artist_mode)
    ranked = rank(batch, rank_query, min_score)
    if min_score and not ranked:
        # The threshold filtered everything: keep a best-effort ranking.
        min_score = 0.0
        ranked = rank(batch, rank_query)
    return store_results(user_id, ranked, query=query, rank_query=rank_query, cursor=cursor, min_score=min_score)


def clamp_page(page: int, total: int, per_page: int = PER_PAGE) -> int:
    """Page number limited to the pages that exist for ``total`` results.

    Filtering keeps the page number from the button, so switching Lossless on
    at page 15 of a long list showed "141-80 из 80" with no tracks.
    """
    last = max(1, (total + per_page - 1) // per_page)
    return min(max(1, page), last)


def _format_results(tracks: list[TrackInfo], page: int, per_page: int, total: int, more: bool = False) -> str:
    """Format search results as numbered list (``more``: the source has more)."""
    if total == 0:
        return "😔 Под этот фильтр ничего не подходит"
    start = (page - 1) * per_page
    plus = "+" if more else ""
    lines = [f"🔍 Результаты {start+1}-{min(start+per_page, total)} из {total}{plus}\n"]

    for i, t in enumerate(tracks[start:start+per_page], start=start+1):
        badge = " ✅" if _is_official(t) else ""
        # HTML-escaped: results are sent with parse_mode=HTML, and a title like
        # "I <3 you" made Telegram reject the whole message.
        lines.append(
            f"{i}. {escape(t.artist)} – {escape(t.title)}{badge} "
            f"{t.duration_str} {t.source_icon} "
            f"{t.size_str} {t.bitrate_str}"
        )

    return "\n".join(lines)


def split_query(query: str) -> tuple[str, str]:
    """Split "Artist - Title" into (artist, title); otherwise ("", query)."""
    for sep in (" — ", " – ", " - ", " -", "- "):
        if sep in query:
            left, _, right = query.partition(sep)
            if left.strip() and right.strip():
                return left.strip(), right.strip()
    return "", query


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
        text=f"🔍 Ищу на всех платформах: <b>{escape(query)}</b>...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None),
        parse_mode="HTML"
    )

    try:
        c_handler = captcha_manager.get_captcha_handler(
            message.bot,
            message.chat.id,
            message.from_user.id
        )

        from app.db.database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT platform_priority FROM user_settings WHERE user_id = ?", (message.from_user.id,))
            data = await row.fetchone()
            platforms = [p.strip() for p in data["platform_priority"].split(",")] if data else list(SOURCES)
        finally:
            await db.close()
        # Priority order breaks ranking ties; sources missing from it still run.
        sources = tuple(dict.fromkeys([p for p in platforms if p in SOURCES] + list(SOURCES)))

        q_artist, q_title = split_query(query)
        session = await new_search(
            message.from_user.id, query, artist=q_artist, title=q_title,
            captcha_handler=c_handler, sources=sources,
        )
        tracks = session.tracks
        if not tracks:
            await status_msg.edit_text("😔 Ничего не найдено")
            return

        artist_name = session.rank_query.artist or tracks[0].artist
        text = _format_results(tracks, page=1, per_page=PER_PAGE, total=len(tracks), more=session.has_more)
        kb = search_results_kb(tracks, page=1, total=len(tracks), per_page=PER_PAGE,
                               artist_name=artist_name, has_more=session.has_more)

        await status_msg.edit_text(text, reply_markup=kb, parse_mode="HTML")

    except Exception as e:
        logger.error("Search failed: %s", e)
        await status_msg.edit_text("❌ Произошла ошибка при поиске. Попробуйте позже.")


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


def _is_official(track: TrackInfo) -> bool:
    """True if the track's uploader looks like an official source / label."""
    return is_official(track)


@router.message(F.text.regexp(r'https?://(?:m\.)?vk\.com/(?:music/playlist/|audio\?z=audio_playlist)(-?\d+)_(\d+)(?:(?:/|%2F|_)([a-zA-Z0-9]+))?'))
async def vk_playlist_url(message: Message) -> None:
    """Handle VK playlist URLs."""
    text = message.text.strip()

    # Extract owner_id, playlist_id, and access_key (if present)
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
