"""/start and /help handlers."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.keyboards.inline import main_reply_kb

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    # Show the music/audiobook switcher in private chats only (keeps groups clean).
    reply_markup = main_reply_kb() if message.chat.type == "private" else None
    await message.answer(
        "🎵 <b>MusicBot</b>\n\n"
        "Я ищу музыку на YouTube, VK и Spotify.\n"
        "Отправь мне:\n"
        "• <b>Текст</b> — поиск по названию\n"
        "• 🎤 <b>Голосовое</b> — распознаю трек (Shazam)\n"
        "• 🔵 <b>Кружок</b> — распознаю из видео\n"
        "• 🎬 <b>Видео</b> — извлеку аудио и найду\n"
        "• 🔗 <b>Ссылку</b> (Instagram, TikTok, ...) — скачаю и распознаю\n\n"
        "📚 <b>Аудиокниги</b> — нажми кнопку «📚 Аудиокниги» внизу и напиши автора/название.\n"
        "📂 Добавь меня в <b>группу с топиками</b> — создам плейлисты!\n\n"
        "/settings — настройки\n"
        "/help — эта справка",
        parse_mode="HTML",
        reply_markup=reply_markup,
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
        "📚 <b>Аудиокниги</b>: кнопка «📚 Аудиокниги» внизу → напиши автора/название.\n"
        "Книга придёт целиком или частями (если она большая).\n\n"
        "📂 <b>Плейлисты</b>: добавь бота в группу с топиками (Forum).\n"
        "Нажимай ❤️🔥😢🎉 на треках — они попадут в соответствующий топик.\n\n"
        "/settings — качество, приоритет платформ\n"
        "/topics — управление топиками (в группе)",
        parse_mode="HTML",
    )
