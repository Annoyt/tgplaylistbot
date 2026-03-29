"""Simplified OAuth handler for VK."""

import re
import logging
from aiogram import F, Router
from aiogram.types import Message
from aiogram.filters import Command

from app.db.database import get_db

router = Router()
logger = logging.getLogger(__name__)

# Official VK for Android / Kate Mobile App ID for audio permissions
APP_ID = "2274003"
AUTH_URL = (
    f"https://oauth.vk.com/authorize?client_id={APP_ID}&display=page&"
    "redirect_uri=https://oauth.vk.com/blank.html&scope=audio,offline&"
    "response_type=token&v=5.131"
)

@router.message(Command("auth"))
async def cmd_auth(message: Message):
    """Send OAuth instructions and link."""
    text = (
        "🔐 <b>Авторизация VK Music</b>\n\n"
        "Для работы поиска и скачивания защищенных треков без 'заглушек' "
        "нужен официальный токен.\n\n"
        "1. Нажмите на ссылку ниже и разрешите доступ:\n"
        f"👉 <a href='{AUTH_URL}'>АВТОРИЗОВАТЬСЯ</a>\n\n"
        "2. Вы попадете на пустую страницу (или увидите предупреждение).\n"
        "3. <b>Скопируйте всю адресную строку</b> этой страницы и пришлите её мне сообщением."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@router.message(F.text.regexp(r"access_token=([a-zA-Z0-9\.\-_]+)"))
async def handle_vk_token_link(message: Message):
    """Extract and save VK token from URL or raw string."""
    text = message.text
    match = re.search(r"access_token=([a-zA-Z0-9\.\-_]+)", text)
    if not match:
        return

    token = match.group(1)
    
    # Optional: extract user_id if present
    user_id_match = re.search(r"user_id=(\d+)", text)
    vk_user_id = user_id_match.group(1) if user_id_match else "unknown"

    db = await get_db()
    try:
        # Save to bot_settings (key-value schema)
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("vk_token", token)
        )
        await db.commit()
        
        await message.answer(
            f"✅ <b>Токен успешно обновлен!</b>\n"
            f"ID пользователя в ВК: <code>{vk_user_id}</code>\n\n"
            "Теперь я буду использовать официальную маскировку для обхода ограничений.",
            parse_mode="HTML"
        )
        logger.info(f"VK Token updated via OAuth link. User ID: {vk_user_id}")
    except Exception as e:
        logger.error(f"Failed to save VK token: {e}")
        await message.answer("❌ Ошибка при сохранении токена. Попробуйте еще раз.")
    finally:
        await db.close()
