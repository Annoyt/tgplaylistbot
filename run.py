"""Entry point: run FastAPI web server + Telegram bot in one event loop."""

import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher

from config import settings
from app.main import app as fastapi_app

# Bot handlers
from bot.handlers.start import router as start_router
from bot.handlers.search import router as search_router
from bot.handlers.recognize import router as recognize_router
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.forum import router as forum_router
from bot.handlers.settings import router as settings_router
from bot.handlers.admin import router as admin_router
from bot.handlers.onboarding import router as onboarding_router

import os
from pathlib import Path

log_dir = Path("data")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)


async def run_web() -> None:
    """Start FastAPI with uvicorn."""
    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def run_bot() -> None:
    """Start Telegram bot polling."""
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled.")
        return

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    # Register routers (order matters: specific first)
    dp.include_router(start_router)
    dp.include_router(onboarding_router)
    dp.include_router(settings_router)
    dp.include_router(admin_router)
    dp.include_router(recognize_router)
    dp.include_router(callbacks_router)
    dp.include_router(forum_router)
    dp.include_router(search_router)  # Last: catches plain text

    from bot.tasks import cleanup_loop
    asyncio.create_task(cleanup_loop(bot))


    logger.info("🤖 Bot starting...")

    # Broadcast startup message to users
    try:
        from app.db.database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT DISTINCT id as chat_id FROM users")
            users = await row.fetchall()
            for user in users:
                try:
                    await bot.send_message(
                        chat_id=user["chat_id"],
                        text="⚙️ Бот обновлён и перезапущен! Добавлены новые функции и исправлены ошибки."
                    )
                except Exception as send_err:
                    pass
        except Exception as query_err:
            logger.error(f"Failed to query users for broadcast: {query_err}")
        finally:
            await db.close()
    except Exception as e:
        logger.error(f"Broadcast failed: {e}")

    await dp.start_polling(bot)

    logger.info("🤖 Bot starting...")
    await dp.start_polling(bot)


async def main() -> None:
    """Run web + bot concurrently."""
    logger.info("🚀 Starting Platform...")

    # Initialize Database
    try:
        from app.db.database import init_db
        await init_db()
        logger.info("✅ Database initialized")
    except Exception as e:
        logger.error(f"❌ Database init failed: {e}")

    await asyncio.gather(
        run_web(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
