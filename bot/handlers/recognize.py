"""Audio/video/link recognition handler: voice, video_note, video, links, audio files."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

from aiogram import F, Router
from aiogram.types import Message

from bot.handlers.search import _format_results, _search_cache
from bot.keyboards.inline import search_results_kb
from config import settings
from services import recognizer
from services import youtube as yt_svc
from services.vk_captcha import captcha_manager

router = Router()
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r'https?://\S+', re.IGNORECASE)


async def _download_tg_file(message: Message, file_id: str) -> str:
    """Download a Telegram file to a temp path."""
    bot = message.bot
    file = await bot.get_file(file_id)
    tmp = tempfile.mktemp(suffix=os.path.splitext(file.file_path or "")[-1] or ".tmp")
    await bot.download_file(file.file_path, tmp)
    return tmp


async def _recognize_and_search(message: Message, audio_path: str) -> None:
    """Recognize track from audio file, then search for full version."""


    # Cross-Topic Routing
    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    status = await message.bot.send_message(
        chat_id=message.chat.id,
        text="🎧 Распознаю трек...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)
    )

    try:
        result = await recognizer.recognize(audio_path, settings.acoustid_api_key)

        if not result:
            await status.edit_text("😔 Ничего не нашлось. Попробуй более длинный фрагмент.")
            return

        await status.edit_text(
            f"🎵 <b>Найден:</b> {result.artist} – {result.title}\n"
            f"🔍 Ищу полную версию...",
            parse_mode="HTML",
        )

        # Search for full version
        c_handler = captcha_manager.get_captcha_handler(
            message.bot, 
            message.chat.id, 
            message.from_user.id
        )
        query = f"{result.artist} {result.title}"
        yt_task = yt_svc.search(query, count=30)
        from services import spotify as sp_svc
        from services import vk_music as vk_svc
        vk_task = vk_svc.search(query, count=30, captcha_handler=c_handler)
        sp_task = sp_svc.search(query, count=30)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)
        all_tracks = []
        for r in results:
            if isinstance(r, list):
                all_tracks.extend(r)

        if not all_tracks:
            await status.edit_text(f"🎵 <b>{result.artist} – {result.title}</b>\nПолная версия не найдена.", parse_mode="HTML")
            return

        user_id = message.from_user.id
        _search_cache[user_id] = all_tracks

        import json
        import uuid
        from dataclasses import asdict

        from app.db.database import get_db
        session_id = str(uuid.uuid4())

        db = await get_db()
        try:
            tracks_json = json.dumps([asdict(t) for t in all_tracks])
            orig_id = message.message_id if getattr(message, 'message_thread_id', None) is None else None

            await db.execute(
                "INSERT INTO search_sessions (session_id, user_id, chat_id, general_msg_ids, query, state_data, original_msg_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, user_id, message.chat.id, "[]", query, tracks_json, orig_id)
            )
            await db.commit()

            # Record the status message so it gets deleted on route
            msg_ids = json.dumps([status.message_id])
            await db.execute("UPDATE search_sessions SET general_msg_ids = ? WHERE session_id = ?", (msg_ids, session_id))
            await db.commit()
        except Exception as db_e:
            logger.error(f"Failed to save session: {db_e}")
        finally:
            await db.close()

        text = f"🎵 <b>{result.artist} – {result.title}</b>\n\n" + _format_results(all_tracks, 1, 10, len(all_tracks))
        kb = search_results_kb(all_tracks, 1, len(all_tracks))
        await status.edit_text(text, reply_markup=kb, parse_mode="HTML")

    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


# ── Voice message ──────────────────────────────────
@router.message(F.voice)
async def handle_voice(message: Message) -> None:


    path = await _download_tg_file(message, message.voice.file_id)
    try:
        await _recognize_and_search(message, path)
    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        if os.path.exists(path):
            os.remove(path)


# ── Video note (circle) ────────────────────────────
@router.message(F.video_note)
async def handle_video_note(message: Message) -> None:


    path = await _download_tg_file(message, message.video_note.file_id)
    try:
        await _recognize_and_search(message, path)
    except Exception as e:
        logger.error(f"Video note handling error: {e}")
        if os.path.exists(path):
            os.remove(path)


# ── Video file (forwarded clips) ───────────────────
@router.message(F.video)
async def handle_video(message: Message) -> None:


    if message.video.file_size and message.video.file_size > 20 * 1024 * 1024:
        await message.answer("⚠️ Видео слишком большое (>20MB). Отправь покороче.")
        return
    path = await _download_tg_file(message, message.video.file_id)
    try:
        await _recognize_and_search(message, path)
    except Exception as e:
        logger.error(f"Video handling error: {e}")
        if os.path.exists(path):
            os.remove(path)


# ── Audio file ─────────────────────────────────────
@router.message(F.audio)
async def handle_audio(message: Message) -> None:


    path = await _download_tg_file(message, message.audio.file_id)
    try:
        await _recognize_and_search(message, path)
    except Exception as e:
        logger.error(f"Audio handling error: {e}")
        if os.path.exists(path):
            os.remove(path)


# ── URL links (Instagram, TikTok, etc.) ────────────
@router.message(F.text.regexp(URL_REGEX))
async def handle_link(message: Message) -> None:

    url = URL_REGEX.search(message.text).group(0)

    # Cross-Topic Routing
    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    status = await message.bot.send_message(
        chat_id=message.chat.id,
        text=f"🔗 Скачиваю видео с {url[:40]}...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)
    )

    try:
        audio_path = await yt_svc.download_from_url(url)
        if not audio_path:
            if "instagram.com" in url:
                await status.edit_text("❌ Не удалось скачать Instagram Reels (Инстаграм блокирует скачивание без авторизации). Нужно прикрепить cookies к боту.")
            else:
                await status.edit_text("❌ Не удалось скачать видео по этой ссылке.")
            return

        await status.edit_text("🎧 Распознаю трек...")
        await _recognize_and_search(message, audio_path)

    except Exception as e:
        logger.error("Link recognition failed: %s", e)
        await status.edit_text(f"❌ Ошибка: {e}")
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)
