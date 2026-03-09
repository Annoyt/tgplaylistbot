"""/settings handler — personal bot preferences."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.db.database import get_db
from app.db.models import UserSettings
from bot.keyboards.inline import settings_kb

router = Router()

PRIORITY_ROTATIONS = [
    "youtube,vk,spotify",
    "vk,youtube,spotify",
    "spotify,youtube,vk",
    "vk,spotify,youtube",
]


async def _get_user_settings(user_id: int) -> UserSettings:
    """Load or create default user settings."""
    db = await get_db()
    try:
        row = await db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        )
        data = await row.fetchone()
        if data:
            return UserSettings(
                user_id=data["user_id"],
                default_quality=data["default_quality"],
                results_per_page=data["results_per_page"],
                platform_priority=data["platform_priority"],
            )
        # Create default
        await db.execute(
            "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()
        return UserSettings(user_id=user_id)
    finally:
        await db.close()


async def _update_setting(user_id: int, key: str, value: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            f"UPDATE user_settings SET {key} = ? WHERE user_id = ?",
            (value, user_id),
        )
        await db.commit()
    finally:
        await db.close()


@router.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    """Show settings menu."""
    s = await _get_user_settings(message.from_user.id)
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"🎵 Качество: <b>{s.default_quality}</b>\n"
        f"📋 Приоритет: <b>{s.platform_priority}</b>\n"
        f"📊 Результатов: <b>{s.results_per_page}</b>",
        reply_markup=settings_kb(s.default_quality, s.platform_priority),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("set:quality:"))
async def cb_quality(callback: CallbackQuery) -> None:
    quality = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    await _update_setting(user_id, "default_quality", quality)
    s = await _get_user_settings(user_id)
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        f"🎵 Качество: <b>{s.default_quality}</b>\n"
        f"📋 Приоритет: <b>{s.platform_priority}</b>\n"
        f"📊 Результатов: <b>{s.results_per_page}</b>",
        reply_markup=settings_kb(s.default_quality, s.platform_priority),
        parse_mode="HTML",
    )
    await callback.answer(f"Качество: {quality}")


@router.callback_query(F.data == "set:priority:cycle")
async def cb_priority(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    s = await _get_user_settings(user_id)
    current_idx = PRIORITY_ROTATIONS.index(s.platform_priority) if s.platform_priority in PRIORITY_ROTATIONS else 0
    new_priority = PRIORITY_ROTATIONS[(current_idx + 1) % len(PRIORITY_ROTATIONS)]
    await _update_setting(user_id, "platform_priority", new_priority)
    s.platform_priority = new_priority
    await callback.message.edit_text(
        "⚙️ <b>Настройки</b>\n\n"
        f"🎵 Качество: <b>{s.default_quality}</b>\n"
        f"📋 Приоритет: <b>{s.platform_priority}</b>\n"
        f"📊 Результатов: <b>{s.results_per_page}</b>",
        reply_markup=settings_kb(s.default_quality, s.platform_priority),
        parse_mode="HTML",
    )
    await callback.answer(f"Приоритет: {new_priority}")


@router.callback_query(F.data == "set:close")
async def cb_close(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("set:header"))
async def cb_header(callback: CallbackQuery) -> None:
    await callback.answer()
