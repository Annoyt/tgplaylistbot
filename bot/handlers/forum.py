"""Forum topics management: auto-create playlists, 👎 voting, /topics command."""

from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, MessageReactionUpdated, FSInputFile,
)

from app.db.database import get_db
from bot.handlers.search import get_cached_tracks
from bot.keyboards.inline import confirm_topic_kb
from services.downloader import download_track, cleanup_file

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_TOPICS = [
    ("❤️", "Избранное"),
    ("🔥", "Энергия"),
    ("😢", "Для души"),
    ("🎉", "Вечеринка"),
    ("💬", "Поиск"),
]


async def _get_topic_map(chat_id: int) -> dict[str, int]:
    """Get emoji -> topic_id mapping for a chat from DB."""
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT emoji, topic_id FROM forum_topics WHERE chat_id = ?", (chat_id,)
        )
        return {row["emoji"]: row["topic_id"] for row in await rows.fetchall()}
    finally:
        await db.close()


async def _save_topic(chat_id: int, topic_id: int, emoji: str, name: str, is_search: bool = False) -> None:
    """Save topic mapping to DB."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO forum_topics (chat_id, topic_id, emoji, name, is_search) VALUES (?,?,?,?,?)",
            (chat_id, topic_id, emoji, name, int(is_search)),
        )
        await db.commit()
    finally:
        await db.close()


@router.message(Command("topics"))
async def cmd_topics(message: Message) -> None:
    """Show and manage forum topics."""
    if not getattr(message.chat, 'is_forum', False):
        await message.answer("⚠️ Эта команда работает только в группах с топиками.")
        return

    topic_map = await _get_topic_map(message.chat.id)
    if not topic_map:
        await message.answer("📂 Топики ещё не созданы. Используй /start_topics для инициализации.")
        return

    lines = ["📂 <b>Топики-плейлисты:</b>\n"]
    for emoji, topic_id in topic_map.items():
        lines.append(f"  {emoji} — topic #{topic_id}")
    lines.append("\nПереименовать топики можно через настройки группы.\nБот ориентируется на <b>иконку-эмодзи</b>, не на название.")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("start_topics"))
async def cmd_start_topics(message: Message) -> None:
    """Auto-create default forum topics in a group."""
    chat = message.chat
    if not getattr(chat, 'is_forum', False):
        await message.answer("⚠️ Включите режим Forum (топики) в настройках группы.")
        return

    bot = message.bot
    created = 0

    for emoji, name in DEFAULT_TOPICS:
        try:
            topic = await bot.create_forum_topic(
                chat_id=chat.id,
                name=f"{emoji} {name}",
                icon_custom_emoji_id=None,
            )
            is_search = (emoji == "💬")
            await _save_topic(chat.id, topic.message_thread_id, emoji, name, is_search)
            created += 1
        except Exception as e:
            logger.warning("Failed to create topic %s %s: %s", emoji, name, e)

    await message.answer(f"✅ Создано {created} топиков-плейлистов! Используй /topics для просмотра.")


async def send_to_topic(callback: CallbackQuery, emoji: str) -> None:
    """Download track and send it to the forum topic matching the emoji."""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    topic_map = await _get_topic_map(chat_id)

    if emoji not in topic_map:
        # Unknown emoji → ask to create topic
        await callback.message.answer(
            f"Топик с эмодзи {emoji} не найден.\nСоздать?",
            reply_markup=confirm_topic_kb(emoji),
        )
        return

    topic_id = topic_map[emoji]
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.message.answer("Результаты устарели, выполни поиск снова.")
        return

    # Download first/selected track
    track = tracks[0]
    status = await callback.message.answer(f"⏳ Скачиваю и отправляю в {emoji}...")

    file_path = None
    try:
        file_path = await download_track(track)
        if not file_path or not os.path.exists(file_path):
            await status.edit_text("❌ Не удалось скачать.")
            return

        await callback.message.bot.send_audio(
            chat_id=chat_id,
            message_thread_id=topic_id,
            audio=FSInputFile(file_path),
            title=track.title,
            performer=track.artist,
            duration=track.duration,
            caption=f"{track.source_icon} {track.artist} – {track.title} [{track.bitrate_str}]",
        )
        await status.edit_text(f"✅ Отправлено в {emoji}!")

    except Exception as e:
        logger.error("Send to topic failed: %s", e)
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        if file_path:
            cleanup_file(file_path)


@router.callback_query(F.data.startswith("topic:create:"))
async def cb_create_topic(callback: CallbackQuery) -> None:
    """Create a new forum topic with the given emoji."""
    emoji = callback.data.split(":", 2)[2]
    chat = callback.message.chat

    try:
        topic = await callback.message.bot.create_forum_topic(
            chat_id=chat.id,
            name=f"{emoji} Новый плейлист",
        )
        await _save_topic(chat.id, topic.message_thread_id, emoji, "Новый плейлист")
        await callback.message.edit_text(f"✅ Топик {emoji} создан!")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка создания: {e}")
    await callback.answer()


@router.callback_query(F.data == "topic:cancel")
async def cb_cancel_topic(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ── 👎 Voting handler ──────────────────────────────
@router.message_reaction()
async def handle_reaction(event: MessageReactionUpdated) -> None:
    """Track 👎 reactions. If all members downvoted, delete the message."""
    if not event.new_reaction:
        return

    has_thumbs_down = any(
        getattr(r, 'emoji', '') == '👎' for r in event.new_reaction
    )
    if not has_thumbs_down:
        return

    try:
        chat = event.chat
        member_count = await event.bot.get_chat_member_count(chat.id)
        # -1 for the bot itself
        threshold = max(1, member_count - 1)

        # Note: Telegram doesn't expose per-reaction counts in real-time.
        # This is a simplified approach — in production, track votes in DB.
        logger.info(
            "👎 reaction on message %d in chat %d (need %d votes)",
            event.message_id, chat.id, threshold,
        )
    except Exception as e:
        logger.warning("Reaction handling error: %s", e)
