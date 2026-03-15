"""Admin utilities: view logs, check status."""

import os
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

from config import settings
from app.db.database import get_db

router = Router()

@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    """Send bot.log file to the user if they are admin."""
    is_admin = False
    db = await get_db()
    try:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (message.from_user.id,))
        res = await row.fetchone()
        if res and res["is_admin"] == 1:
            is_admin = True
    finally:
        await db.close()

    if not is_admin:
        return

    log_path = "data/bot.log"
    if not os.path.exists(log_path):
        await message.answer("❌ Файл логов (data/bot.log) не найден.")
        return

    try:
        await message.answer_document(
            document=FSInputFile(log_path),
            caption="📜 Последние логи работы бота."
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка отправки логов: {e}")

@router.message(Command("setadmin"))
async def cmd_setadmin(message: Message) -> None:
    """Make the current user an admin if they provide the correct web admin password."""
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or args[1] != settings.admin_password:
        return

    db = await get_db()
    try:
        # Ensure user exists first
        await db.execute("INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)", (message.from_user.id, message.from_user.username))

        await db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (message.from_user.id,))
        await db.commit()
        await message.answer("✅ Теперь ты администратор бота. Доступна команда /logs")
    finally:
        await db.close()
