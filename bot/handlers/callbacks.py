"""Callback query handler: pagination, filters, downloads, playlist emoji routing."""

from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

from bot.handlers.search import get_cached_tracks, sort_tracks, filter_lossless, _format_results
from bot.keyboards.inline import search_results_kb, track_detail_kb, confirm_topic_kb
from services.downloader import download_track, cleanup_file

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery) -> None:
    """Handle page navigation."""
    parts = callback.data.split(":")
    page = int(parts[1])
    sort_by = parts[2] if len(parts) > 2 else ""
    lossless = bool(int(parts[3])) if len(parts) > 3 else False

    user_id = callback.from_user.id
    from bot.handlers.search import get_cached_tracks
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.answer("Результаты устарели, выполни поиск снова.")
        return

    # Apply filters
    filtered = tracks
    if lossless:
        filtered = filter_lossless(filtered)
    if sort_by:
        filtered = sort_tracks(filtered, sort_by)

    text = _format_results(filtered, page, 10, len(filtered))
    kb = search_results_kb(filtered, page, len(filtered), sort_by=sort_by, lossless_only=lossless)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter(callback: CallbackQuery) -> None:
    """Handle filter/sort buttons."""
    parts = callback.data.split(":")
    filter_type = parts[1]
    value = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 1

    user_id = callback.from_user.id
    from bot.handlers.search import get_cached_tracks
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.answer("Результаты устарели.")
        return

    sort_by = ""
    lossless = False

    if filter_type == "br":
        sort_by = value
        tracks = sort_tracks(tracks, sort_by)
    elif filter_type == "lossless":
        lossless = bool(int(value))
        if lossless:
            tracks = filter_lossless(tracks)
    elif filter_type == "title":
        sort_by = "title"
        tracks = sort_tracks(tracks, "title")

    text = _format_results(tracks, page, 10, len(tracks))
    kb = search_results_kb(tracks, page, len(tracks), sort_by=sort_by, lossless_only=lossless)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(callback: CallbackQuery) -> None:
    """Handle download request."""
    parts = callback.data.split(":")
    track_idx = int(parts[1])
    quality = parts[2]

    user_id = callback.from_user.id
    from bot.handlers.search import get_cached_tracks
    tracks = get_cached_tracks(user_id)

    if track_idx < 0 or track_idx >= len(tracks):
        await callback.answer("Трек не найден.")
        return

    track = tracks[track_idx]
    await callback.answer(f"⏳ Скачиваю {track.artist} – {track.title}...")
    status = await callback.message.answer(f"⏳ Скачиваю: {track.artist} – {track.title} ({quality})...")

    try:
        file_path = await download_track(track, quality)
        if not file_path or not os.path.exists(file_path):
            await status.edit_text("❌ Не удалось скачать трек.")
            return

        file_size = os.path.getsize(file_path)
        if file_size > 49 * 1024 * 1024:
            await status.edit_text("❌ Файл слишком большой для Telegram (>50MB).")
            cleanup_file(file_path)
            return


        caption_text = f"{track.source_icon} {track.artist} – {track.title}\n👤 #{callback.from_user.id}"
        if callback.from_user.username:
            caption_text += f" (@{callback.from_user.username})"

        audio_msg = await callback.message.answer_audio(
            audio=FSInputFile(file_path),
            title=track.title,
            performer=track.artist,
            duration=track.duration,
            caption=caption_text,

        )

        # Save to pending_tracks
        if callback.message.chat.type != "private":
            from app.db.database import get_db
            import json
            from dataclasses import asdict
            db = await get_db()
            try:
                # We need to save the search message ID to delete it later
                # We will keep the search message so users can download other tracks
                # Or we can link it. The PRD says "search message and original request are kept until vote passes".
                # Find original msg id from session
                orig_id = None
                row = await db.execute("SELECT original_msg_id FROM search_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
                res = await row.fetchone()
                if res:
                    orig_id = res["original_msg_id"]

                # Add the column to pending_tracks if not exists
                try:
                    await db.execute("ALTER TABLE pending_tracks ADD COLUMN original_msg_id INTEGER")
                    await db.commit()
                except:
                    pass

                await db.execute(
                    "INSERT INTO pending_tracks (chat_id, audio_msg_id, search_msg_id, user_id, track_json, original_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (callback.message.chat.id, audio_msg.message_id, callback.message.message_id, user_id, json.dumps(asdict(track)), orig_id)
                )
                await db.commit()
            except Exception as dbe:
                logger.error(f"Failed to save pending track: {dbe}")
            finally:
                await db.close()

        await status.delete()

    except Exception as e:
        logger.error("Download failed: %s", e)
        await status.edit_text(f"❌ Ошибка скачивания: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            cleanup_file(file_path)


@router.callback_query(F.data == "back_to_list")
async def cb_back(callback: CallbackQuery) -> None:
    """Back to search results."""
    user_id = callback.from_user.id
    from bot.handlers.search import get_cached_tracks
    tracks = get_cached_tracks(user_id)
    if tracks:
        text = _format_results(tracks, 1, 10, len(tracks))
        kb = search_results_kb(tracks, 1, len(tracks))
        await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("playlist:"))
async def cb_playlist(callback: CallbackQuery) -> None:
    """Handle playlist emoji buttons — route track to forum topic."""
    parts = callback.data.split(":")
    emoji = parts[1]
    track_idx = int(parts[2]) if len(parts) > 2 else 0

    if emoji == "new":
        # We need state to save track_idx
        from aiogram.fsm.context import FSMContext

        # We will dispatch to FSM by modifying the handler signature slightly,
        # but aiogram automatically injects state if it's in the signature.
        # Since we can't change signature without breaking aiogram if we don't import FSMContext,
        # let's just save it into a generic DB table or cache for now to keep it simple and robust.

        # Simpler approach: save it in a small memory dict or DB
        from bot.handlers.forum import _new_playlist_cache
        _new_playlist_cache[callback.from_user.id] = track_idx

        await callback.message.answer("📁 Отправь мне эмодзи для нового плейлиста:")
        await callback.answer()
        return

    chat = callback.message.chat
    # Check if we're in a forum group
    if not getattr(chat, 'is_forum', False):
        await callback.answer("📂 Плейлисты работают в группах с топиками. Добавь бота в группу-форум!", show_alert=True)
        return

    await callback.answer(f"Отправляю в топик {emoji}...")
    # Delegate to forum handler logic
    from bot.handlers.forum import send_to_topic
    await send_to_topic(callback, emoji, track_idx)

@router.callback_query(F.data.startswith("select_track:"))
async def cb_select_track(callback: CallbackQuery) -> None:
    """Handle track selection from search results."""
    parts = callback.data.split(":")
    track_idx = int(parts[1])

    user_id = callback.from_user.id
    from bot.handlers.search import get_cached_tracks
    tracks = get_cached_tracks(user_id)

    if track_idx < 0 or track_idx >= len(tracks):
        await callback.answer("Трек не найден.", show_alert=True)
        return

    track = tracks[track_idx]

    # Show track details and options
    text = f"🎵 Выбран трек:\n<b>{track.artist} – {track.title}</b>"

    from bot.keyboards.inline import track_detail_kb
    is_private = callback.message.chat.type == "private"
    await callback.message.edit_text(text, reply_markup=track_detail_kb(track_idx, is_private=is_private), parse_mode="HTML")
    await callback.answer()
