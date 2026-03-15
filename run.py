"""Entry point: run FastAPI web server + Telegram bot in one event loop."""

import asyncio
import logging
import traceback

import uvicorn
from aiogram import Bot, Dispatcher

from config import settings
from app.main import app as fastapi_app
from app.db.database import get_db

# Bot handlers
from bot.handlers.start import router as start_router
from bot.handlers.search import router as search_router
from bot.handlers.recognize import router as recognize_router
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.forum import router as forum_router
from bot.handlers.settings import router as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def get_db_setting(key: str, default: str = "") -> str:
    """Fetch setting from database."""
    try:
        db = await get_db()
        async with db.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            await db.close()
            if row and row["value"]:
                return row["value"]
    except Exception as e:
        logger.error(f"Failed to fetch setting {key}: {e}")
    return default


async def run_web() -> None:
    """Start FastAPI with uvicorn."""
    try:
        config = uvicorn.Config(
            app=fastapi_app,
            host="0.0.0.0",
            port=8000,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()
    except Exception as e:
        logger.error(f"Web server failed: {e}")
        traceback.print_exc()


async def run_bot() -> None:
    """Start Telegram bot polling."""
    try:
        # Check DB first, fallback to .env
        token = await get_db_setting("telegram_bot_token", settings.telegram_bot_token)

        if not token or len(token) < 10:
            logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled.")
            return

        bot = Bot(token=token)
        dp = Dispatcher()

        # Register routers (order matters: specific first)
        dp.include_router(start_router)
        dp.include_router(settings_router)
        dp.include_router(recognize_router)
        dp.include_router(callbacks_router)
        dp.include_router(forum_router)
        dp.include_router(search_router)  # Last: catches plain text

        logger.info("🤖 Bot starting...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot polling failed: {e}")
        traceback.print_exc()


async def main() -> None:
    """Run web + bot concurrently."""
    logger.info("🚀 Starting Platform...")

    await asyncio.gather(
        run_web(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
