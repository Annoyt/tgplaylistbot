"""Handles user joining, onboarding, and nickname configuration."""

import logging
from aiogram import Router, F
from aiogram.types import Message, ChatMemberUpdated
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, Command

from app.db.database import get_db

router = Router()
logger = logging.getLogger(__name__)

async def _ensure_user(user_id: int, username: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
            (user_id, username)
        )
        await db.commit()
    finally:
        await db.close()

async def _update_nickname(user_id: int, nickname: str) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE users SET nickname = ? WHERE id = ?", (nickname, user_id))
        await db.commit()
    finally:
        await db.close()

async def _get_nickname(user_id: int, first_name: str) -> str:
    db = await get_db()
    try:
        row = await db.execute("SELECT nickname FROM users WHERE id = ?", (user_id,))
        data = await row.fetchone()
        if data and data["nickname"]:
            return data["nickname"]
        return first_name
    finally:
        await db.close()

@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_user_join(event: ChatMemberUpdated) -> None:
    """Triggered when a user (or the bot) joins a group."""
    new_member = event.new_chat_member.user

    # If the bot itself was added
    if new_member.id == event.bot.id:
        if not getattr(event.chat, 'is_forum', False):
            await event.bot.send_message(
                event.chat.id,
                "Привет! 👋 Чтобы я работал корректно и мог сортировать музыку по плейлистам, "
                "пожалуйста, включите <b>Темы (Форум)</b> в настройках этой группы.\n\n"
                "После включения тем, напишите команду /start_topics для создания стартовых плейлистов."
            )
        return

    # A normal user joined
    await _ensure_user(new_member.id, new_member.username or "")

    # In groups with topics, send to the General topic if possible
    # Telegram implicitly sets message_thread_id=None for the General topic
    await event.bot.send_message(
        event.chat.id,
        f"Добро пожаловать, {new_member.first_name}! 🎉\n\n"
        "Как мне к тебе обращаться? Установи свой никнейм командой:\n"
        "<code>/nickname ТвойНик</code>",
        parse_mode="HTML"
    )

@router.message(Command("nickname"))
async def cmd_nickname(message: Message) -> None:
    """Allow user to set their preferred nickname."""
    args = message.text.split(maxsplit=1)

    await _ensure_user(message.from_user.id, message.from_user.username or "")

    if len(args) < 2:
        current_nick = await _get_nickname(message.from_user.id, message.from_user.first_name)
        await message.answer(
            f"Сейчас я обращаюсь к тебе так: <b>{current_nick}</b>\n\n"
            "Чтобы изменить, напиши: <code>/nickname НовоеИмя</code>",
            parse_mode="HTML"
        )
        return

    new_nickname = args[1][:50] # Limit length
    await _update_nickname(message.from_user.id, new_nickname)
    await message.answer(f"Отлично! Теперь я буду обращаться к тебе: <b>{new_nickname}</b>", parse_mode="HTML")
