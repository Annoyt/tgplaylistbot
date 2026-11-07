"""Bot signature appended to captions of media the bot sends."""

from aiogram import Bot


async def with_signature(caption: str, bot: Bot, chat_type: str) -> str:
    """Append "🤖 @bot" so forwarded media leads people back to the bot.

    Private chats only: in a group the bot is already right there.
    """
    if chat_type != "private":
        return caption
    me = await bot.me()  # cached by aiogram after the first call
    signature = f"🤖 @{me.username}"
    return f"{caption}\n\n{signature}" if caption else signature
