"""Forum topics management: auto-create playlists, 👎 voting, /topics command."""

from __future__ import annotations
from aiogram.dispatcher.event.bases import UNHANDLED

import logging
import os

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, Message, MessageReactionUpdated, FSInputFile,
)

from app.db.database import get_db
from bot.handlers.search import get_cached_tracks
from bot.keyboards.inline import confirm_topic_kb
from services.downloader import download_track, cleanup_file

router = Router()
logger = logging.getLogger(__name__)

DEFAULT_TOPICS = [
    ("❤️", "Избранное"),
    ("🔥", "Энергия"),
    ("😢", "Для души"),
    ("🎉", "Вечеринка"),
    ("💬", "Поиск"),
]


async def _get_topic_map(chat_id: int) -> dict[str, int]:
    """Get emoji -> topic_id mapping for a chat from DB."""
    db = await get_db()
    try:
        rows = await db.execute(
            "SELECT emoji, topic_id FROM forum_topics WHERE chat_id = ?", (chat_id,)
        )
        return {row["emoji"]: row["topic_id"] for row in await rows.fetchall()}
    finally:
        await db.close()


async def _save_topic(chat_id: int, topic_id: int, emoji: str, name: str, is_search: bool = False) -> None:
    """Save topic mapping to DB."""
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO forum_topics (chat_id, topic_id, emoji, name, is_search) VALUES (?,?,?,?,?)",
            (chat_id, topic_id, emoji, name, int(is_search)),
        )
        await db.commit()
    finally:
        await db.close()


@router.message(Command("topics"))
async def cmd_topics(message: Message) -> None:
    """Show and manage forum topics."""
    if not getattr(message.chat, 'is_forum', False):
        await message.answer("⚠️ Эта команда работает только в группах с топиками.")
        return

    topic_map = await _get_topic_map(message.chat.id)
    if not topic_map:
        await message.answer("📂 Топики ещё не созданы. Используй /start_topics для инициализации.")
        return

    lines = ["📂 <b>Топики-плейлисты:</b>\n"]
    for emoji, topic_id in topic_map.items():
        lines.append(f"  {emoji} — topic #{topic_id}")
    lines.append("\nПереименовать топики можно через настройки группы.\nБот ориентируется на <b>иконку-эмодзи</b>, не на название.")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("start_topics"))
async def cmd_start_topics(message: Message) -> None:
    """Auto-create default forum topics in a group."""
    chat = message.chat
    if not getattr(chat, 'is_forum', False):
        await message.answer("⚠️ Включите режим Forum (топики) в настройках группы.")
        return

    bot = message.bot
    created = 0

    for emoji, name in DEFAULT_TOPICS:
        try:
            topic = await bot.create_forum_topic(
                chat_id=chat.id,
                name=f"{emoji} {name}",
                icon_custom_emoji_id=None,
            )
            is_search = (emoji == "💬")
            await _save_topic(chat.id, topic.message_thread_id, emoji, name, is_search)
            created += 1
        except Exception as e:
            logger.warning("Failed to create topic %s %s: %s", emoji, name, e)

    await message.answer(f"✅ Создано {created} топиков-плейлистов! Используй /topics для просмотра.")



async def send_to_topic(callback: CallbackQuery, emoji: str, track_idx: int = 0) -> None:
    """Download track and send it to the forum topic matching the emoji, then clean up general chat."""
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    topic_map = await _get_topic_map(chat_id)

    if emoji not in topic_map:
        # Unknown emoji → ask to create topic
        await callback.message.answer(
            f"Топик с эмодзи {emoji} не найден.\nСоздать?",
            reply_markup=confirm_topic_kb(emoji, track_idx),
        )
        return

    topic_id = topic_map[emoji]
    tracks = get_cached_tracks(user_id)
    if not tracks or track_idx >= len(tracks):
        await callback.message.answer("Результаты устарели, выполни поиск снова.")
        return

    track = tracks[track_idx]

    status = await callback.message.answer(f"⏳ Скачиваю и отправляю в {emoji}...")

    file_path = None
    try:
        from services.downloader import download_track, cleanup_file

        # Get quality setting
        from app.db.database import get_db
        db = await get_db()
        quality = "mp3_320"
        try:
            row = await db.execute("SELECT default_quality FROM user_settings WHERE user_id = ?", (user_id,))
            res = await row.fetchone()
            if res:
                quality = res["default_quality"]
        finally:
            await db.close()

        file_path = await download_track(track, quality)
        if not file_path or not os.path.exists(file_path):
            await status.edit_text("❌ Не удалось скачать.")
            return

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        undo_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏪ Отменить (Undo)", callback_data=f"undo:{chat_id}:{topic_id}")
        ]])


        sent_msg = await callback.message.bot.send_audio(
            chat_id=chat_id,
            message_thread_id=topic_id,
            audio=FSInputFile(file_path),
            title=track.title,
            performer=track.artist,
            duration=track.duration,
            caption=f"{track.source_icon} {track.artist} – {track.title} [{track.bitrate_str}]",
            reply_markup=undo_kb
        )

        # Track message in DB
        db = await get_db()
        try:
            await db.execute("INSERT INTO topic_messages (chat_id, topic_id, message_id) VALUES (?, ?, ?)",
                             (chat_id, topic_id, sent_msg.message_id))
            await db.commit()
        finally:
            await db.close()


        # Clean up spam from General Topic
        try:
            await callback.message.delete()
            await status.delete()
        except Exception as e:
            logger.warning(f"Failed to delete search messages: {e}")

    except Exception as e:
        logger.error("Send to topic failed: %s", e)
        await status.edit_text(f"❌ Ошибка: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            from services.downloader import cleanup_file
            cleanup_file(file_path)
@router.callback_query(F.data.startswith("topic:create:"))
async def cb_create_topic(callback: CallbackQuery) -> None:
    """Create a new forum topic with the given emoji."""
    parts = callback.data.split(":")
    emoji = parts[2]
    track_idx = int(parts[3]) if len(parts) > 3 else 0
    chat = callback.message.chat

    try:
        from bot.handlers.search import get_cached_tracks
        tracks = get_cached_tracks(callback.from_user.id)
        name = "Новый плейлист"
        if tracks and track_idx < len(tracks):
            # Attempt to name after genre or artist if known, else artist
            name = tracks[track_idx].artist[:10] + "..."

        topic = await callback.message.bot.create_forum_topic(
            chat_id=chat.id,
            name=f"{emoji} {name}",
        )
        await _save_topic(chat.id, topic.message_thread_id, emoji, name)
        await callback.message.edit_text(f"✅ Топик {emoji} создан! Отправляю трек...")

        # After creating topic, send the track
        await send_to_topic(callback, emoji, track_idx)
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка создания: {e}")
    await callback.answer()


@router.callback_query(F.data == "topic:cancel")
async def cb_cancel_topic(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()


# ── 👎 Voting handler ──────────────────────────────

async def _check_empty_topic(bot, chat_id: int, topic_id: int) -> None:
    """Check if a topic has become empty after message deletion. If so, delete the topic to keep the group clean."""
    from app.db.database import get_db
    db = await get_db()
    try:
        # Check how many messages we tracked in this topic
        row = await db.execute("SELECT COUNT(*) as count FROM topic_messages WHERE chat_id = ? AND topic_id = ?", (chat_id, topic_id))
        res = await row.fetchone()

        # If count drops to 0, close and delete the topic
        if res and res["count"] == 0:
            try:
                # We can't delete the General topic (message_thread_id=None)
                if topic_id is not None:
                    # Telegram lets bots close or delete topics if they have the right permissions
                    await bot.delete_forum_topic(chat_id, topic_id)
                    # Remove from our DB
                    await db.execute("DELETE FROM forum_topics WHERE chat_id = ? AND topic_id = ?", (chat_id, topic_id))
                    await db.commit()
            except Exception as e:
                logger.warning(f"Could not delete empty topic {topic_id}: {e}")
    finally:
        await db.close()

@router.message_reaction()
async def handle_reaction(event: MessageReactionUpdated) -> None:
    """Process reactions on tracks for voting."""
    chat = event.chat
    from app.db.database import get_db
    from bot.handlers.admin import get_global_settings

    db = await get_db()
    try:
        # 1. Check if it's a pending track awaiting a vote
        row = await db.execute("SELECT * FROM pending_tracks WHERE chat_id = ? AND audio_msg_id = ?", (chat.id, event.message_id))
        pending_track = await row.fetchone()

        if pending_track:


            # If user removed their reaction
            voter_id = event.user.id if event.user else (event.actor_chat.id if hasattr(event, "actor_chat") and event.actor_chat else 0)
            if not voter_id:
                return

            if not event.new_reaction:
                await db.execute("DELETE FROM track_votes WHERE msg_id = ? AND user_id = ?", (event.message_id, voter_id))
            else:
                # User added or changed their reaction
                emoji = getattr(event.new_reaction[0], 'emoji', getattr(event.new_reaction[0], 'custom_emoji_id', ''))
                if emoji:
                    await db.execute("INSERT OR REPLACE INTO track_votes (msg_id, user_id, emoji) VALUES (?, ?, ?)", (event.message_id, voter_id, emoji))
            await db.commit()

            # Check if threshold is met to schedule routing
            s = await get_global_settings()
            member_count = await event.bot.get_chat_member_count(chat.id)
            threshold = max(1, int(member_count * (s.vote_threshold_pct / 100.0)))

            row = await db.execute("SELECT emoji, COUNT(*) as c FROM track_votes WHERE msg_id = ? GROUP BY emoji ORDER BY c DESC LIMIT 1", (event.message_id,))
            top_vote = await row.fetchone()

            if top_vote and top_vote["c"] >= threshold:

                # Update scheduled time (if it wasn't already scheduled)
                import time
                route_time = int(time.time()) + s.vote_interval_sec
                try:
                    await db.execute("UPDATE pending_tracks SET route_at = ?, route_emoji = ? WHERE id = ? AND (route_at = 0 OR route_at IS NULL)",
                                     (route_time, top_vote["emoji"], pending_track["id"]))
                    await db.commit()
                except Exception:
                    pass
            else:
                # If votes drop below threshold, cancel the routing
                try:
                    await db.execute("UPDATE pending_tracks SET route_at = 0 WHERE id = ?", (pending_track["id"],))
                    await db.commit()
                except Exception:
                    pass
            return

        # 2. Check if it's an ALREADY ROUTED track in a topic being downvoted
        row = await db.execute("SELECT topic_id FROM topic_messages WHERE chat_id = ? AND message_id = ?", (chat.id, event.message_id))
        res = await row.fetchone()
        if res:
            voter_id = event.user.id if event.user else (event.actor_chat.id if hasattr(event, "actor_chat") and event.actor_chat else 0)
            if not voter_id:
                return

            await db.execute("CREATE TABLE IF NOT EXISTS track_votes (msg_id INTEGER, user_id INTEGER, emoji TEXT, UNIQUE(msg_id, user_id))")
            await db.commit()

            has_thumbs_down = any(getattr(r, 'emoji', '') == '👎' for r in event.new_reaction) if event.new_reaction else False

            if not has_thumbs_down:
                # User removed downvote or changed it to something else
                await db.execute("DELETE FROM track_votes WHERE msg_id = ? AND user_id = ? AND emoji = '👎'", (event.message_id, voter_id))
                await db.commit()
            else:
                # Add downvote
                await db.execute("INSERT OR REPLACE INTO track_votes (msg_id, user_id, emoji) VALUES (?, ?, '👎')", (event.message_id, voter_id))
                await db.commit()

            # Check threshold
            s = await get_global_settings()
            member_count = await event.bot.get_chat_member_count(chat.id)
            threshold = max(1, int(member_count * (s.vote_threshold_pct / 100.0)))

            row = await db.execute("SELECT COUNT(*) as c FROM track_votes WHERE msg_id = ? AND emoji = '👎'", (event.message_id,))
            downvotes = await row.fetchone()

            if downvotes and downvotes["c"] >= threshold:
                try:
                    await event.bot.delete_message(chat.id, event.message_id)
                except Exception:
                    pass
                await db.execute("DELETE FROM topic_messages WHERE chat_id = ? AND message_id = ?", (chat.id, event.message_id))
                await db.execute("DELETE FROM track_votes WHERE msg_id = ?", (event.message_id,))
                await db.commit()

                await _check_empty_topic(event.bot, chat.id, res["topic_id"])
    except Exception as e:
        logger.warning("Reaction handling error: %s", e)
    finally:
        await db.close()

@router.callback_query(F.data.startswith("undo:"))
async def cb_undo(callback: CallbackQuery) -> None:
    """Handle Undo: delete the track from the specific topic and notify the user."""
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    topic_id = int(parts[2])

    try:
        await callback.message.delete()

        from app.db.database import get_db
        db = await get_db()
        try:
            await db.execute("DELETE FROM topic_messages WHERE chat_id = ? AND message_id = ?", (chat_id, callback.message.message_id))
            await db.commit()
        finally:
            await db.close()

        # Check if topic is empty
        await _check_empty_topic(callback.message.bot, chat_id, topic_id)

        # Restore the menu in General chat
        user_id = callback.from_user.id
        from bot.handlers.search import get_cached_tracks, _format_results
        from bot.keyboards.inline import search_results_kb

        tracks = get_cached_tracks(user_id)
        if tracks:
            text = _format_results(tracks, page=1, per_page=10, total=len(tracks))
            kb = search_results_kb(tracks, page=1, total=len(tracks), per_page=10)

            # Send the search results back to the general chat
            await callback.message.bot.send_message(
                chat_id=chat_id,
                text=f"⏪ Действие отменено пользователем {callback.from_user.first_name}.\n\n{text}",
                reply_markup=kb,
                message_thread_id=None # General topic
            )

        await callback.answer("✅ Отменено. Трек удалён из топика, результаты возвращены в Общий чат.")
    except Exception as e:
        logger.error(f"Failed to undo: {e}")
        await callback.answer("❌ Ошибка отмены", show_alert=True)

# In-memory dictionary to hold track_idx when creating a new playlist via text emoji message
_new_playlist_cache = {}

# We need a message handler to catch the emoji sent by the user for a new playlist
@router.message(F.text & F.chat.type.in_({"group", "supergroup"}))
async def handle_new_playlist_emoji(message: Message) -> None:
    """Catch emoji text input to create a new playlist for a waiting track."""
    user_id = message.from_user.id
    if user_id not in _new_playlist_cache:
        return UNHANDLED

    track_idx = _new_playlist_cache.pop(user_id)
    emoji = message.text.strip()

    if len(emoji) > 5:
        await message.answer("Пожалуйста, отправь только один эмодзи 😅")
        _new_playlist_cache[user_id] = track_idx # put back
        return

    from bot.keyboards.inline import confirm_topic_kb
    await message.answer(
        f"Топик с эмодзи {emoji} не найден.\nСоздать?",
        reply_markup=confirm_topic_kb(emoji, track_idx),
    )
