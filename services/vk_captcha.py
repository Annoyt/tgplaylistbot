import asyncio
import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)

CAPTCHA_TIMEOUT_SEC = 120


class CaptchaManager:
    def __init__(self):
        # user_id -> {"event": threading.Event, "answer": str | None}
        self.active_sessions: Dict[int, Dict] = {}

    def get_captcha_handler(self, bot, chat_id: int, user_id: int):
        """Returns a synchronous callback for vk_api.

        Must be called from async code: the bot's loop is captured here because
        vk_api invokes the callback in an executor thread, where
        ``asyncio.get_event_loop()`` raises on Python 3.12 — that left the session
        registered forever and every later message of the user was taken for a
        captcha answer.
        """
        loop = asyncio.get_running_loop()

        def handler(captcha):
            logger.info(f"CAPTCHA triggered for user {user_id}")

            event = threading.Event()
            session_data = {"event": event, "answer": None}
            self.active_sessions[user_id] = session_data

            async def notify_user():
                try:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=captcha.get_url(),
                        caption="💠 <b>Требуется ввод капчи VK!</b>\nПожалуйста, введите текст с картинки ниже.",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Failed to send captcha image: {e}")
                    event.set()  # Don't hang forever if bot fails

            try:
                asyncio.run_coroutine_threadsafe(notify_user(), loop)
                # Block the current executor thread (never the event loop)
                if event.wait(timeout=CAPTCHA_TIMEOUT_SEC) and session_data["answer"]:
                    logger.info("CAPTCHA answer received for user %s", user_id)
                    return captcha.try_again(session_data["answer"])
                logger.warning(f"CAPTCHA for user {user_id} timed out or failed.")
                return None
            finally:
                # Solved, timed out or crashed: stop treating the user's messages
                # as answers. Only drop our own session, not a newer one.
                if self.active_sessions.get(user_id) is session_data:
                    del self.active_sessions[user_id]

        return handler

    def solve_captcha(self, user_id: int, answer: str):
        """Called by the bot's message handler when a user provides the text."""
        if user_id in self.active_sessions:
            session = self.active_sessions[user_id]
            session["answer"] = answer
            session["event"].set()
            return True
        return False

# Global instance
captcha_manager = CaptchaManager()
