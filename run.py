"""Entry point: run FastAPI web server + Telegram bot in one event loop."""

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher

from app.main import app as fastapi_app
from bot.handlers.admin import router as admin_router
from bot.handlers.audiobook import router as audiobook_router
from bot.handlers.callbacks import router as callbacks_router
from bot.handlers.captcha import router as captcha_router
from bot.handlers.forum import router as forum_router
from bot.handlers.onboarding import router as onboarding_router
from bot.handlers.recognize import router as recognize_router
from bot.handlers.search import router as search_router
from bot.handlers.settings import router as settings_router
from bot.handlers.auth import router as auth_router

# Bot handlers
from bot.handlers.start import router as start_router
from config import settings

log_dir = Path("data")
log_dir.mkdir(exist_ok=True)
log_file = log_dir / "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        # Rotate so bot.log can't grow unbounded: 5MB × 3 backups (~20MB cap).
        RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ]
)
logger = logging.getLogger(__name__)

_web_server: uvicorn.Server | None = None


async def run_web() -> None:
    """Start FastAPI with uvicorn."""
    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
    global _web_server
    _web_server = uvicorn.Server(config)
    await _web_server.serve()


async def run_bot() -> None:
    """Start Telegram bot polling."""
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled.")
        return

    # Route through a self-hosted Bot API server if configured (lifts the 50 MB
    # send limit to 2 GB — needed to deliver whole audiobooks).
    if settings.local_bot_api:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        api_server = TelegramAPIServer.from_base(
            settings.telegram_api_base_url.rstrip("/"), is_local=True
        )
        bot = Bot(
            token=settings.telegram_bot_token,
            session=AiohttpSession(api=api_server),
        )
        logger.info("📡 Using local Bot API server at %s (2 GB uploads)", settings.telegram_api_base_url)
    else:
        bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()

    # Register routers (order matters: specific first)
    dp.include_router(start_router)
    dp.include_router(onboarding_router)
    dp.include_router(captcha_router) # Before search to catch answers
    dp.include_router(settings_router)
    dp.include_router(admin_router)
    dp.include_router(recognize_router)
    dp.include_router(callbacks_router)
    dp.include_router(forum_router)
    dp.include_router(auth_router)  # Handles OAuth links
    dp.include_router(audiobook_router)  # Book mode + 📚/🎵 toggle (before plain text)
    dp.include_router(search_router)  # Last: catches plain text

    from bot.tasks import cleanup_loop
    asyncio.create_task(cleanup_loop(bot))
    from bot.tasks import playlist_queue_loop
    asyncio.create_task(playlist_queue_loop(bot))
    from bot.tasks import disk_cleanup_loop
    asyncio.create_task(disk_cleanup_loop(bot))
    from bot.tasks import ytdlp_update_loop
    asyncio.create_task(ytdlp_update_loop())

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

    # SIGTERM is caught by only one of uvicorn / aiogram (whoever installed its
    # handler last), so with a plain gather() the other kept running and
    # `docker stop` hung for 10s until SIGKILL (python is PID 1, so the default
    # SIGTERM action never fires). When either side stops, stop the other.
    web = asyncio.create_task(run_web())
    tasks = [web]
    if settings.telegram_bot_token:
        tasks.append(asyncio.create_task(run_bot()))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        if task is web and _web_server is not None:
            # Graceful exit; cancelling uvicorn logs a CancelledError traceback.
            _web_server.should_exit = True
        else:
            task.cancel()
    if pending:
        _, stuck = await asyncio.wait(pending, timeout=5)
        for task in stuck:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()  # re-raise a crash so the container restarts


if __name__ == "__main__":
    asyncio.run(main())
