"""Simplified OAuth handler for VK."""

import re
import logging
from aiogram import F, Router
from aiogram.types import Message
from aiogram.filters import Command

from app.db.database import get_db

router = Router()
logger = logging.getLogger(__name__)

@router.message(Command("auth"))
async def cmd_auth(message: Message):
    """Send OAuth instructions and alternative direct auth link."""
    text = (
        "🔐 <b>Продвинутая Авторизация VK (Без 'заглушек')</b>\n\n"
        "ВКонтакте блокирует выдачу официальных токенов через обычный браузер. "
        "Чтобы получить полновесные треки (например, Metallica), действуйте так:\n\n"
        "1. Скопируйте ссылку ниже и вставьте в адресную строку браузера (с телефона или ПК):\n\n"
        "<code>https://oauth.vk.com/token?grant_type=password&client_id=2274003&client_secret=hHbZxrka2uZ6jB1inYsH&username=ВАШ_ЛОГИН&password=ВАШ_ПАРОЛЬ&v=5.131</code>\n\n"
        "2. Вместо ВАШ_ЛОГИН и ВАШ_ПАРОЛЬ впишите свои данные.\n"
        "3. Перейдите по ссылке. Вы увидите белый экран с текстом: <code>{\"access_token\":\"vk1.a...</code>\n"
        "4. Скопируйте <b>только сам токен</b> (внутри кавычек).\n\n"
        "👇 Затем пришлите мне его в таком формате:\n"
        "<code>access_token=vk1.a.ТУТ_ВАШ_СКОПИРОВАННЫЙ_КОД</code>"
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

@router.message(Command("vklogin"))
async def cmd_vklogin(message: Message):
    """Direct login to VK via official Android app."""
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("⚠️ <b>Неверный формат команды!</b>\nПожалуйста, отправьте команду в формате: <code>/vklogin ваш_номер ваш_пароль</code>\n\nПример: <code>/vklogin +79991234567 mySecretPass</code>", parse_mode="HTML")
        return
    
    login = parts[1]
    password = parts[2]
    
    db = await get_db()
    try:
        # Clear existing unprivileged token and set official login/password
        await db.execute("DELETE FROM bot_settings WHERE key = 'vk_token'")
        await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", ("vk_login", login))
        await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", ("vk_password", password))
        await db.commit()
        
        await message.answer("✅ <b>Учетные данные сохранены!</b>\n\nЯ удалил старые браузерные токены. Теперь при любом поиске я проведу 100% официальную авторизацию с сервера под видом Android-приложения, и <b>никаких 'заглушек' в 25 секунд больше не будет!</b>", parse_mode="HTML")
        
        # Security: delete user's message containing the password
        try:
            await message.delete()
            logger.info("VK Login executed. User's password message was successfully hidden/deleted.")
        except Exception:
            logger.warning("Could not delete user's message to hide password (bot might lack rights).")
    
    except Exception as e:
        await message.answer(f"❌ <b>Критическая ошибка базы данных:</b> {e}", parse_mode="HTML")
    finally:
        await db.close()
