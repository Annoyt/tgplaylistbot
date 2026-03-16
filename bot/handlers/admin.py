"""Admin utilities: view logs, check status."""

import os
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

from config import settings
from app.db.database import get_db
from app.db.models import GlobalSettings

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



async def get_global_settings() -> GlobalSettings:
    db = await get_db()
    try:
        row = await db.execute("SELECT key, value FROM bot_settings")
        data = {r["key"]: r["value"] for r in await row.fetchall()}

        # Parse or default
        return GlobalSettings(
            vote_threshold_pct=int(data.get("vote_threshold_pct", 25)),
            vote_interval_sec=int(data.get("vote_interval_sec", 60)),
            download_delay_sec=int(data.get("download_delay_sec", 3)),
            msg_ttl_days=int(data.get("msg_ttl_days", 7)),
            forward_mode=data.get("forward_mode", "resend")
        )
    finally:
        await db.close()

async def update_global_setting(key: str, value: str) -> None:
    db = await get_db()
    try:
        await db.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    finally:
        await db.close()

@router.message(Command("admin_settings"))
async def cmd_admin_settings(message: Message) -> None:
    """View global bot settings."""
    db = await get_db()
    is_admin = False
    try:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (message.from_user.id,))
        res = await row.fetchone()
        if res and res["is_admin"] == 1:
            is_admin = True
    finally:
        await db.close()

    if not is_admin:
        return

    s = await get_global_settings()
    await message.answer(
        "⚙️ **Глобальные настройки бота**\n\n"
        f"Голосование (процент): `{s.vote_threshold_pct}`%\n"
        f"Задержка проверки голоса: `{s.vote_interval_sec}` сек\n"
        f"Задержка скачивания: `{s.download_delay_sec}` сек\n"
        f"Удаление спама через: `{s.msg_ttl_days}` дней\n"
        f"Режим отправки: `{s.forward_mode}` (resend/forward)\n\n"
        "Чтобы изменить, используй команду:\n`/set_global <ключ> <значение>`",
        parse_mode="Markdown"
    )

@router.message(Command("set_global"))
async def cmd_set_global(message: Message) -> None:
    """Change a global bot setting."""
    db = await get_db()
    is_admin = False
    try:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (message.from_user.id,))
        res = await row.fetchone()
        if res and res["is_admin"] == 1:
            is_admin = True
    finally:
        await db.close()

    if not is_admin:
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("❌ Формат: `/set_global <ключ> <значение>`", parse_mode="Markdown")
        return

    key, value = args[1], args[2]

    valid_keys = ["vote_threshold_pct", "vote_interval_sec", "download_delay_sec", "msg_ttl_days", "forward_mode"]
    if key not in valid_keys:
        await message.answer(f"❌ Неизвестный ключ. Доступные: {', '.join(valid_keys)}")
        return

    await update_global_setting(key, value)
    await message.answer(f"✅ Настройка `{key}` изменена на `{value}`", parse_mode="Markdown")
