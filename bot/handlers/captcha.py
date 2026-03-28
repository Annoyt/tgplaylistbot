"""Handler for VK captcha text responses."""

from aiogram import F, Router
from aiogram.types import Message

from services.vk_captcha import captcha_manager

router = Router()

@router.message(F.text & ~F.text.startswith("/"))
async def handle_captcha_reply(message: Message):
    """
    Check if the user is currently in a captcha session.
    If so, treat the message as the answer.
    """
    user_id = message.from_user.id
    
    # Check if this user is expected to solve a captcha
    if user_id in captcha_manager.active_sessions:
        answer = message.text.strip()
        if captcha_manager.solve_captcha(user_id, answer):
            await message.reply("✅ Ответ принят, продолжаю операцию...")
            # The search/download task in the other thread will now resume
        else:
            await message.reply("❌ Не удалось обработать ответ. Попробуйте снова.")
    # If not in session, ignore (let other routers handle it)
    return
