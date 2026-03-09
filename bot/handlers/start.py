"""/start and /help handlers."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🎵 <b>MusicBot</b>\n\n"
        "Я ищу музыку на YouTube, VK и Spotify.\n"
        "Отправь мне:\n"
        "• <b>Текст</b> — поиск по названию\n"
        "• 🎤 <b>Голосовое</b> — распознаю трек (Shazam)\n"
        "• 🔵 <b>Кружок</b> — распознаю из видео\n"
        "• 🎬 <b>Видео</b> — извлеку аудио и найду\n"
        "• 🔗 <b>Ссылку</b> (Instagram, TikTok, ...) — скачаю и распознаю\n\n"
        "📂 Добавь меня в <b>группу с топиками</b> — создам плейлисты!\n\n"
        "/settings — настройки\n"
        "/help — эта справка",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Команды</b>\n\n"
        "Просто напиши название трека — найду на всех платформах.\n\n"
        "🎤 Голосовое / 🔵 Кружок / 🎬 Видео\n"
        "→ Распознаю трек через Shazam\n\n"
        "🔗 Ссылка (Instagram, TikTok, YouTube Shorts...)\n"
        "→ Скачаю, распознаю, найду полную версию\n\n"
        "📂 <b>Плейлисты</b>: добавь бота в группу с топиками (Forum).\n"
        "Нажимай ❤️🔥😢🎉 на треках — они попадут в соответствующий топик.\n\n"
        "/settings — качество, приоритет платформ\n"
        "/topics — управление топиками (в группе)",
        parse_mode="HTML",
    )
