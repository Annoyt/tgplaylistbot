"""Audio/video/link recognition handler: voice, video_note, video, links, audio files."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import uuid
from collections import OrderedDict

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot.handlers.search import _format_results, _search_cache
from bot.keyboards.inline import link_video_kb, search_results_kb, video_res_kb
from config import settings
from services import recognizer
from services import youtube as yt_svc
from services.vk_captcha import captcha_manager

router = Router()
logger = logging.getLogger(__name__)

URL_REGEX = re.compile(r'https?://\S+', re.IGNORECASE)

# Short token -> original URL, so video-download callbacks stay within Telegram's
# 64-byte callback_data limit. Capped to avoid unbounded growth.
_link_urls: "OrderedDict[str, str]" = OrderedDict()
_LINK_CACHE_MAX = 1000


def _cache_link(url: str) -> str:
    """Store a URL under a short token for later video-download callbacks."""
    token = uuid.uuid4().hex[:10]
    _link_urls[token] = url
    while len(_link_urls) > _LINK_CACHE_MAX:
        _link_urls.popitem(last=False)
    return token


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
            f"✅ <b>Распознан трек:</b> {result.artist} – {result.title}",
            parse_mode="HTML",
        )

        # Search for full version
        c_handler = captcha_manager.get_captcha_handler(
            message.bot, 
            message.chat.id, 
            message.from_user.id
        )
        # Wrap artist and title in quotes for more precise search
        query = f'"{result.artist}" "{result.title}"'
        yt_task = yt_svc.search(query, count=30)
        from services import spotify as sp_svc
        from services import vk_music as vk_svc
        vk_task = vk_svc.search(query, count=30, captcha_handler=c_handler)
        sp_task = sp_svc.search(query, count=30)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)
        all_tracks: list[TrackInfo] = []

        for r in results:
            if isinstance(r, list):
                all_tracks.extend(r)

        if not all_tracks:
            await status.edit_text(f"🎵 <b>{result.artist} – {result.title}</b>\nПолная версия не найдена.", parse_mode="HTML")
            return

        user_id = message.from_user.id
        # Rank by closeness to the recognized official name. Drop weak matches,
        # but if the threshold filters everything keep a best-effort ranking.
        from bot.handlers.search import rank_by_relevance
        ranked = rank_by_relevance(all_tracks, result.artist, result.title, min_score=0.45)
        all_tracks = ranked or rank_by_relevance(all_tracks, result.artist, result.title)

        # 2. Update memory cache
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

            text = _format_results(all_tracks, 1, 10, len(all_tracks))
            kb = search_results_kb(all_tracks, 1, len(all_tracks), artist_name=result.artist)
            
            # Send results as a NEW message
            results_msg = await message.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                reply_markup=kb,
                parse_mode="HTML",
                message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)
            )

            # Record both messages for cleanup
            msg_ids = json.dumps([status.message_id, results_msg.message_id])
            await db.execute("UPDATE search_sessions SET general_msg_ids = ? WHERE session_id = ?", (msg_ids, session_id))
            await db.commit()
        except Exception as db_e:
            logger.error(f"Failed to save session: {db_e}")
        finally:
            await db.close()

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

    thread_id = None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)

    status = await message.bot.send_message(
        chat_id=message.chat.id,
        text=f"🔗 Скачиваю видео с {url[:40]}...",
        message_thread_id=thread_id,
    )

    # Offer the full video alongside music recognition (the user can pick a
    # resolution). Sent up-front so it's available even if recognition fails.
    token = _cache_link(url)
    await message.bot.send_message(
        chat_id=message.chat.id,
        text="🎬 Нужно само видео, а не музыка?",
        reply_markup=link_video_kb(token),
        message_thread_id=thread_id,
    )

    try:
        audio_path = await yt_svc.download_from_url(url)
        if not audio_path:
            if "instagram.com" in url:
                if settings.cookies_path.exists():
                    await status.edit_text(
                        "❌ Не удалось скачать из Instagram даже с cookies.\n\n"
                        "Возможные причины:\n"
                        "• cookies устарели — выгрузите свежий `cookies.txt` и пришлите боту заново\n"
                        "• Reels приватный или удалён\n"
                        "• Instagram временно ограничил доступ — попробуйте позже",
                        parse_mode="Markdown",
                    )
                else:
                    await status.edit_text(
                        "❌ Instagram блокирует скачивание без авторизации.\n\n"
                        "Админу нужно прислать боту файл `cookies.txt` (формат Netscape), "
                        "экспортированный из браузера, где выполнен вход в Instagram.",
                        parse_mode="Markdown",
                    )
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


# ── Video download from a pasted link ──────────────
@router.callback_query(F.data.startswith("lv:"))
async def cb_link_video(callback: CallbackQuery) -> None:
    """Show the resolution picker for a link's video."""
    token = callback.data.split(":", 1)[1]
    if token not in _link_urls:
        await callback.answer("Ссылка устарела — пришлите её заново.", show_alert=True)
        return
    await callback.message.edit_text(
        "🎬 В каком качестве скачать видео?",
        reply_markup=video_res_kb(token),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lvr:"))
async def cb_link_video_res(callback: CallbackQuery) -> None:
    """Download the link's video at the chosen resolution and send it."""
    _, token, res = callback.data.split(":")
    url = _link_urls.get(token)
    if not url:
        await callback.answer("Ссылка устарела — пришлите её заново.", show_alert=True)
        return

    max_height = None if res == "best" else int(res)
    label = "Лучшее" if max_height is None else f"{max_height}p"
    await callback.answer(f"⏳ Скачиваю видео ({label})...")
    await callback.message.edit_text(f"⏳ Скачиваю видео ({label})...")

    video_path = None
    try:
        video_path = await yt_svc.download_video_from_url(url, max_height=max_height)
        if not video_path or not os.path.exists(video_path):
            await callback.message.edit_text(
                "❌ Не удалось скачать видео (формат недоступен или превышен лимит)."
            )
            return

        size = os.path.getsize(video_path)
        if size > settings.send_limit_bytes:
            limit_mb = settings.send_limit_bytes // (1024 * 1024)
            await callback.message.edit_text(
                f"❌ Видео слишком большое для отправки (>{limit_mb}MB). "
                "Попробуйте меньшее разрешение."
            )
            return

        from aiogram.types import FSInputFile

        await callback.message.edit_text(f"📤 Отправляю видео ({label})...")
        await callback.message.answer_video(
            video=FSInputFile(video_path),
            caption=f"🎬 {label}",
        )
        await callback.message.delete()
    except Exception as e:
        logger.error("Video download failed: %s", e)
        await callback.message.edit_text(f"❌ Ошибка при скачивании видео: {e}")
    finally:
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
