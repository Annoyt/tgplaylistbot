import asyncio
import logging
import threading
from typing import Dict

logger = logging.getLogger(__name__)

class CaptchaManager:
    def __init__(self):
        # user_id -> threading.Event and result_value
        self.active_sessions: Dict[int, Dict] = {}

    def get_captcha_handler(self, bot, chat_id: int, user_id: int):
        """Returns a synchronous callback for vk_api."""
        
        def handler(captcha):
            logger.info(f"CAPTCHA triggered for user {user_id}")
            
            # Create a synchronization event for this thread
            event = threading.Event()
            session_data = {"event": event, "answer": None}
            self.active_sessions[user_id] = session_data

            # Use the bot loop to send the captcha image
            loop = asyncio.get_event_loop()
            
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
                    event.set() # Don't hang forever if bot fails

            # Schedule notification in the main loop
            asyncio.run_coroutine_threadsafe(notify_user(), loop)

            # Block the current executor thread (for up to 120s)
            if event.wait(timeout=120):
                answer = session_data.get("answer")
                if answer:
                    logger.info(f"CAPTCHA answer received: {answer}")
                    return captcha.try_again(answer)
            
            logger.warning(f"CAPTCHA for user {user_id} timed out or failed.")
            del self.active_sessions[user_id]
            return None

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
