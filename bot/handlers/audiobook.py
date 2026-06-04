"""Audiobook section: a separate search mode toggled by a reply-keyboard button.

Music search still owns plain text; here a per-user "book mode" (entered via the
📚 button) routes the next messages to the audiobook providers instead. Books
are downloaded as one file and delivered whole (local Bot API server, up to
2 GB) or split into <50 MB parts when on the cloud API.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, FSInputFile, Message

from app.db.models import TrackInfo
from bot.keyboards.inline import (
    BTN_AUDIOBOOKS,
    BTN_MUSIC,
    audiobook_detail_kb,
    audiobook_results_kb,
    main_reply_kb,
)
from services import audiobook as ab_service

router = Router()
logger = logging.getLogger(__name__)

PER_PAGE = 6

# Users currently in audiobook search mode (toggled by the 📚 button).
_book_mode: set[int] = set()
# Last audiobook search results per user (user_id -> list[TrackInfo]).
_MAX_CACHE = 500
_book_cache: "OrderedDict[int, list[TrackInfo]]" = OrderedDict()


class InBookMode(BaseFilter):
    """Matches only when the user has switched to audiobook mode."""

    async def __call__(self, message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in _book_mode


def _fmt_dur(seconds: int) -> str:
    h, rem = divmod(max(0, seconds), 3600)
    m = rem // 60
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def _source_icon(source: str) -> str:
    return {"youtube": "▶️YT", "librivox": "📚LV"}.get(source, source)


def _format_books(books: list[TrackInfo], page: int, per_page: int) -> str:
    total = len(books)
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    lines = [f"📚 Аудиокниги {start + 1}-{end} из {total}\n"]
    for i in range(start, end):
        b = books[i]
        lines.append(f"{i + 1}. {b.artist} — {b.title}\n     🕒 {_fmt_dur(b.duration)}  {_source_icon(b.source)}")
    return "\n".join(lines)


# ── Mode toggle (reply-keyboard buttons) ───────────────────────────────────
@router.message(Command("audiobooks"))
@router.message(F.text == BTN_AUDIOBOOKS)
async def enter_book_mode(message: Message) -> None:
    _book_mode.add(message.from_user.id)
    await message.answer(
        "📚 Режим аудиокниг включён.\nНапиши автора или название книги — поищу на всех источниках.\n\n"
        "Чтобы вернуться к музыке, нажми «🎵 Музыка».",
        reply_markup=main_reply_kb(),
    )


@router.message(F.text == BTN_MUSIC)
async def exit_book_mode(message: Message) -> None:
    _book_mode.discard(message.from_user.id)
    await message.answer(
        "🎵 Режим музыки. Напиши название трека.",
        reply_markup=main_reply_kb(),
    )


# ── Book search (only in private chats while in book mode) ──────────────────
@router.message(
    InBookMode(),
    F.chat.type == "private",
    F.text & ~F.text.startswith("/") & ~F.text.startswith("http"),
)
async def book_search(message: Message) -> None:
    query = message.text.strip()
    if len(query) < 2:
        return

    status = await message.answer(f"📚 Ищу аудиокниги: <b>{query}</b>…", parse_mode="HTML")
    try:
        books = await ab_service.search(query)
    except Exception as e:
        logger.error("Audiobook search failed: %s", e)
        await status.edit_text("❌ Ошибка поиска аудиокниг. Попробуй позже.")
        return

    if not books:
        await status.edit_text("😔 Аудиокниги не найдены. Уточни автора или название.")
        return

    user_id = message.from_user.id
    _book_cache[user_id] = books
    if len(_book_cache) > _MAX_CACHE:
        _book_cache.popitem(last=False)

    await status.edit_text(
        _format_books(books, 1, PER_PAGE),
        reply_markup=audiobook_results_kb(len(books), 1, PER_PAGE),
    )


# ── Pagination / selection ─────────────────────────────────────────────────
@router.callback_query(F.data.startswith("bpage:"))
async def cb_book_page(callback: CallbackQuery) -> None:
    arg = callback.data.split(":", 1)[1]
    if arg == "noop":
        await callback.answer()
        return
    page = int(arg)
    books = _book_cache.get(callback.from_user.id)
    if not books:
        await callback.answer("Результаты устарели, выполни поиск снова.")
        return
    await callback.message.edit_text(
        _format_books(books, page, PER_PAGE),
        reply_markup=audiobook_results_kb(len(books), page, PER_PAGE),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bback:"))
async def cb_book_back(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":", 1)[1])
    books = _book_cache.get(callback.from_user.id)
    if not books:
        await callback.answer("Результаты устарели.")
        return
    await callback.message.edit_text(
        _format_books(books, page, PER_PAGE),
        reply_markup=audiobook_results_kb(len(books), page, PER_PAGE),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bsel:"))
async def cb_book_select(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    idx = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 1
    books = _book_cache.get(callback.from_user.id)
    if not books or idx >= len(books):
        await callback.answer("❌ Сессия поиска истекла.")
        return
    b = books[idx]
    src = {"youtube": "YouTube", "librivox": "LibriVox"}.get(b.source, b.source)
    text = (
        f"📖 {b.artist} — {b.title}\n\n"
        f"🕒 Длительность: {_fmt_dur(b.duration)}\n"
        f"📥 Источник: {src}"
    )
    await callback.message.edit_text(
        text, reply_markup=audiobook_detail_kb(idx, source_name=src, page=page)
    )
    await callback.answer()


# ── Download + delivery ────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("bdl:"))
async def cb_book_download(callback: CallbackQuery) -> None:
    idx = int(callback.data.split(":")[1])
    books = _book_cache.get(callback.from_user.id)
    if not books or idx >= len(books):
        await callback.answer("❌ Сессия поиска истекла.")
        return
    book = books[idx]

    await callback.answer("⏳ Скачиваю аудиокнигу…")
    status = await callback.message.answer(
        f"⏳ Скачиваю: {book.artist} — {book.title}\n"
        f"🕒 {_fmt_dur(book.duration)} — это может занять несколько минут…"
    )

    file_path: str | None = None
    paths: list[str] = []
    try:
        file_path = await ab_service.download(book)
        if not file_path or not os.path.exists(file_path):
            await status.edit_text("❌ Не удалось скачать аудиокнигу.")
            return

        await status.edit_text("📦 Готовлю файл к отправке…")
        paths = await ab_service.prepare_parts(file_path)
        n = len(paths)

        for i, p in enumerate(paths, start=1):
            if not os.path.exists(p) or os.path.getsize(p) == 0:
                continue
            dur = await ab_service.ffprobe_duration(p)
            caption = f"📖 {book.artist} — {book.title}"
            title = book.title
            if n > 1:
                caption += f"\nЧасть {i}/{n}"
                title = f"{book.title} — ч.{i}/{n}"
                await status.edit_text(f"📤 Отправляю часть {i}/{n}…")

            safe_artist = book.artist.replace("/", "_").replace("\\", "_")
            safe_title = book.title.replace("/", "_").replace("\\", "_")
            suffix = f" p{i:02d}" if n > 1 else ""
            filename = f"{safe_artist} - {safe_title}{suffix}.mp3"

            await callback.message.answer_audio(
                audio=FSInputFile(p, filename=filename),
                title=title[:64],
                performer=book.artist[:64],
                duration=dur if dur > 0 else None,
                caption=caption,
            )

        await status.delete()

    except Exception as e:
        logger.error("Audiobook delivery failed: %s", e)
        try:
            await status.edit_text(f"❌ Ошибка при отправке аудиокниги: {e}")
        except Exception:
            pass
    finally:
        for p in set(paths + ([file_path] if file_path else [])):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
