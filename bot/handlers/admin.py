"""Admin utilities: view logs, check status."""

import asyncio
import hmac
import logging
import os
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

from config import settings
from app.db.database import get_db, get_db_ctx
from app.db.models import GlobalSettings

router = Router()
logger = logging.getLogger(__name__)


async def is_bot_admin(user_id: int) -> bool:
    async with get_db_ctx() as db:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,))
        res = await row.fetchone()
    return bool(res and res["is_admin"] == 1)


async def admin_password_ok(candidate: str) -> bool:
    """Check a /setadmin password against the dashboard password.

    A password set from the dashboard (bcrypt hash in bot_settings) wins over
    ADMIN_PASSWORD. The public default ADMIN_PASSWORD never counts: with it,
    anyone typing "/setadmin change-me" became a bot admin.
    """
    from app.auth.service import env_password_is_default, verify_password

    async with get_db_ctx() as db:
        row = await db.execute("SELECT value FROM bot_settings WHERE key = 'admin_password_hash'")
        res = await row.fetchone()
    if res and res["value"]:
        return await asyncio.to_thread(verify_password, candidate, res["value"])
    if env_password_is_default():
        return False
    return hmac.compare_digest(candidate.encode(), settings.admin_password.encode())


@router.message(Command("logs"))
async def cmd_logs(message: Message) -> None:
    """Send bot.log file to the user if they are admin."""
    if not await is_bot_admin(message.from_user.id):
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
    if len(args) < 2:
        return
    # The password travels in the chat (possibly a group): hide it right away.
    try:
        await message.delete()
    except Exception:
        pass
    if not await admin_password_ok(args[1].strip()):
        logger.warning("Failed /setadmin attempt by user %s", message.from_user.id)
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



@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Show usage stats (admin only): users, searches, downloads, top queries."""
    db = await get_db()
    try:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (message.from_user.id,))
        res = await row.fetchone()
        if not (res and res["is_admin"] == 1):
            return

        async def scalar(sql: str) -> int:
            cur = await db.execute(sql)
            r = await cur.fetchone()
            return r["n"] if r else 0

        users = await scalar("SELECT COUNT(*) AS n FROM users")
        admins = await scalar("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1")
        searches = await scalar("SELECT COUNT(*) AS n FROM search_sessions")
        downloads = await scalar("SELECT COUNT(*) AS n FROM cached_tracks")
        downloads7 = await scalar(
            "SELECT COUNT(*) AS n FROM cached_tracks WHERE created_at >= datetime('now','-7 days')"
        )
        cur = await db.execute(
            "SELECT query, COUNT(*) AS c FROM search_sessions "
            "GROUP BY lower(query) ORDER BY c DESC, max(created_at) DESC LIMIT 10"
        )
        top_rows = await cur.fetchall()
    finally:
        await db.close()

    top = "\n".join(
        f"  {i}. {r['query'][:40]} — {r['c']}" for i, r in enumerate(top_rows, 1)
    ) or "  —"
    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: <b>{users}</b> (админов: {admins})\n"
        f"🔍 Поисков (в истории сессий): <b>{searches}</b>\n"
        f"⬇️ Скачано треков (кэш): <b>{downloads}</b> (за 7 дней: {downloads7})\n\n"
        f"🔝 <b>Топ запросов:</b>\n{top}",
        parse_mode="HTML",
    )


async def get_global_settings() -> GlobalSettings:
    db = await get_db()
    try:
        row = await db.execute("SELECT key, value FROM bot_settings")
        data = {r["key"]: r["value"] for r in await row.fetchall()}

        # Parse or default. A bad stored value (e.g. "None" from a half-filled
        # dashboard form) must not raise: every download, the routing loop and
        # the dashboard itself read these settings.
        d = GlobalSettings()
        mode = data.get("forward_mode", d.forward_mode)
        return GlobalSettings(
            vote_threshold_pct=_int_or(data.get("vote_threshold_pct"), d.vote_threshold_pct),
            vote_interval_sec=_int_or(data.get("vote_interval_sec"), d.vote_interval_sec),
            download_delay_sec=_int_or(data.get("download_delay_sec"), d.download_delay_sec),
            msg_ttl_days=_int_or(data.get("msg_ttl_days"), d.msg_ttl_days),
            forward_mode=mode if mode in FORWARD_MODES else d.forward_mode,
        )
    finally:
        await db.close()


FORWARD_MODES = ("resend", "forward")
INT_SETTINGS = ("vote_threshold_pct", "vote_interval_sec", "download_delay_sec", "msg_ttl_days")


def _int_or(value, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def setting_error(key: str, value: str) -> str | None:
    """Why ``value`` is invalid for global setting ``key``, or None if it's fine."""
    if key in INT_SETTINGS:
        return None if value.strip().isdigit() else "нужно целое неотрицательное число"
    if key == "forward_mode":
        return None if value in FORWARD_MODES else "допустимо: " + ", ".join(FORWARD_MODES)
    return "неизвестный ключ"

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
    if not await is_bot_admin(message.from_user.id):
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
    if not await is_bot_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3:
        await message.answer("❌ Формат: `/set_global <ключ> <значение>`", parse_mode="Markdown")
        return

    key, value = args[1], args[2]

    valid_keys = [*INT_SETTINGS, "forward_mode"]
    if key not in valid_keys:
        await message.answer(f"❌ Неизвестный ключ. Доступные: {', '.join(valid_keys)}")
        return
    if err := setting_error(key, value):
        await message.answer(f"❌ Неверное значение для {key}: {err}")
        return

    await update_global_setting(key, value.strip())
    await message.answer(f"✅ Настройка `{key}` изменена на `{value}`", parse_mode="Markdown")


@router.message(Command("clear_cache"))
async def cmd_clear_cache(message: Message) -> None:
    """Clear the cached_tracks table."""
    if not await is_bot_admin(message.from_user.id):
        return

    db = await get_db()
    try:
        await db.execute("DELETE FROM cached_tracks")
        await db.commit()
        await message.answer("♻️ **Кеш поиска очищен.** Теперь все треки будут скачиваться заново с применением последних исправлений.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка при очистке кеша: {e}")
    finally:
        await db.close()

@router.message(F.document)
async def handle_cookies_upload(message: Message) -> None:
    """Accept cookies.txt from admin to help yt-dlp bypass Instagram/YouTube blocks."""
    if message.document.file_name != "cookies.txt":
        return

    if not await is_bot_admin(message.from_user.id):
        await message.answer(
            "🔒 Загрузка `cookies.txt` доступна только администраторам.\n\n"
            "Используй `/setadmin <пароль>`, чтобы получить права.",
            parse_mode="Markdown",
        )
        return

    # Save into the persisted data volume so it survives container restarts.
    dest = settings.cookies_path
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        await message.bot.download(message.document, str(dest))
        # Sanity-check: Netscape cookie jars start with a recognizable header.
        head = dest.read_text(encoding="utf-8", errors="ignore").lstrip()[:64].lower()
        if not head.startswith("# netscape") and "# http cookie file" not in head:
            await message.answer(
                "⚠️ Файл сохранён, но не похож на cookies в формате Netscape.\n\n"
                "Экспортируйте cookies из браузера (например, расширением "
                "«Get cookies.txt LOCALLY») и пришлите файл `cookies.txt` заново.",
                parse_mode="Markdown",
            )
            return
        await message.answer(
            "🍪 Файл `cookies.txt` сохранён в постоянное хранилище.\n\n"
            "Теперь он используется для обхода блокировок Instagram и YouTube "
            "и переживёт перезапуск бота.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка при сохранении файла: {e}")

