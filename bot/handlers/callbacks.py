"""Callback query handler: pagination, filters, downloads, playlist emoji routing."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import F, Router
from aiogram.types import CallbackQuery, FSInputFile

from app.db.database import get_db, get_db_ctx
from bot.handlers.search import _format_results, filter_lossless, get_cached_tracks, sort_tracks
from bot.keyboards.inline import search_results_kb, track_detail_kb
from services.cache import generate_track_hash, get_cached_file_id, save_cached_file_id
from services.downloader import cleanup_file, download_track

router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("page:"))
async def cb_page(callback: CallbackQuery) -> None:
    """Handle page navigation."""
    parts = callback.data.split(":")
    page = int(parts[1])
    sort_by = parts[2] if len(parts) > 2 else ""
    lossless = bool(int(parts[3])) if len(parts) > 3 else False

    user_id = callback.from_user.id
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.answer("Результаты устарели, выполни поиск снова.")
        return

    # Apply filters
    filtered = tracks
    if lossless:
        filtered = filter_lossless(filtered)
    if sort_by:
        filtered = sort_tracks(filtered, sort_by)

    # Maintain artist context for the 'Popular' button
    artist_name = filtered[0].artist if filtered else None

    text = _format_results(filtered, page, 10, len(filtered))
    kb = search_results_kb(filtered, page, len(filtered), sort_by=sort_by, lossless_only=lossless, artist_name=artist_name)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("filter:"))
async def cb_filter(callback: CallbackQuery) -> None:
    """Handle filter/sort buttons."""
    parts = callback.data.split(":")
    filter_type = parts[1]
    value = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 1

    user_id = callback.from_user.id
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.answer("Результаты устарели.")
        return

    sort_by = ""
    lossless = False

    if filter_type == "br":
        sort_by = value
        tracks = sort_tracks(tracks, sort_by)
    elif filter_type == "lossless":
        lossless = bool(int(value))
        if lossless:
            tracks = filter_lossless(tracks)
    elif filter_type == "title":
        sort_by = "title"
        tracks = sort_tracks(tracks, "title")

    # Maintain artist context for the 'Popular' button
    artist_name = tracks[0].artist if tracks else None

    text = _format_results(tracks, page, 10, len(tracks))
    kb = search_results_kb(tracks, page, len(tracks), sort_by=sort_by, lossless_only=lossless, artist_name=artist_name)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


async def get_audio_duration_ffprobe(file_path: str) -> int:
    """Get the duration of an audio file using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", file_path
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return int(float(stdout.decode().strip()))
    except Exception as e:
        logger.warning(f"ffprobe duration detection failed: {e}")
    return 0


@router.callback_query(F.data.startswith("dl:"))
async def cb_download(callback: CallbackQuery) -> None:
    """Handle download request."""
    parts = callback.data.split(":")
    track_idx = int(parts[1])
    quality = parts[2]
    force_redownload = len(parts) > 3 and parts[3] == "1"

    user_id = callback.from_user.id
    tracks = get_cached_tracks(user_id)

    if track_idx < 0 or track_idx >= len(tracks):
        await callback.answer("Трек не найден.")
        return

    track = tracks[track_idx]
    await callback.answer(f"⏳ Скачиваю {track.artist} – {track.title}...")
    status = await callback.message.answer(f"⏳ Скачиваю: {track.artist} – {track.title} ({quality})...")

    try:
        # Caption will be formatted after download to include real size/bitrate
        caption_text = ""

        # 1. Check Cache First
        if force_redownload:
            logger.info(f"Force redownload requested for {track.artist} - {track.title}")
            track_hash = generate_track_hash(track.artist, track.title, track.duration)
            async with get_db_ctx() as db:
                await db.execute("DELETE FROM cached_tracks WHERE artist_title_hash = ?", (track_hash,))
                await db.commit()
            cached_file_id = None
        else:
            cached_file_id = await get_cached_file_id(track.artist, track.title, track.duration)

        audio_msg = None
        file_path = None

        if cached_file_id:
            # For cached files, use estimated size/bitrate info if possible
            cached_caption = f"🎵 {track.artist} – {track.title} {track.source_icon}\n"
            cached_caption += f"👤 #{callback.from_user.id}"
            if callback.from_user.username:
                cached_caption += f" (@{callback.from_user.username})"

            logger.info(f"Using cached file ID for {track.artist} - {track.title}")
            try:
                audio_msg = await callback.message.answer_audio(
                    audio=cached_file_id,
                    caption=cached_caption,
                )
            except Exception as e:
                logger.warning(f"Failed to send cached audio (maybe deleted?): {e}")
                cached_file_id = None # Fallback to download

        # 2. Download if not cached or cache sending failed
        if not cached_file_id:
            file_path = await download_track(
                track, 
                quality,
                bot=callback.message.bot,
                chat_id=callback.message.chat.id,
                user_id=callback.from_user.id
            )
            if not file_path or not os.path.exists(file_path):
                await status.edit_text("❌ Не удалось скачать трек.")
                return

            file_size = os.path.getsize(file_path)
            if file_size > 49 * 1024 * 1024:
                await status.edit_text("❌ Файл слишком большой для Telegram (>50MB).")
                cleanup_file(file_path)
                return

            # Calculate real bitrate for metadata
            file_size_bytes = os.path.getsize(file_path)
            real_bitrate = int(file_size_bytes * 8 / track.duration / 1000) if track.duration > 0 else 0
            bitrate_label = f" {{{real_bitrate}kbps}}" if real_bitrate > 0 else ""
            
            # Format real size string
            size_mb = file_size_bytes / (1024 * 1024)
            size_label = f" {{{size_mb:.1f}MB}}"
            
            # Final Caption in requested format: Artist - Title {bitrate} {size} Icon
            caption_text = f"🎵 {track.artist} – {track.title}{bitrate_label}{size_label} {track.source_icon}\n"
            caption_text += f"👤 #{callback.from_user.id}"
            if callback.from_user.username:
                caption_text += f" (@{callback.from_user.username})"

            # Get REAL duration from the final file to avoid UI jumps
            real_duration = await get_audio_duration_ffprobe(file_path)
            duration_to_use = real_duration if real_duration > 0 else track.duration

            # Ensure safe filename for Telegram
            safe_artist = track.artist.replace("/", "_").replace("\\", "_")
            safe_title = track.title.replace("/", "_").replace("\\", "_")
            file_extension = os.path.splitext(file_path)[1] or ".mp3"
            telegram_filename = f"{safe_artist} - {safe_title}{file_extension}"

            audio_msg = await callback.message.answer_audio(
                audio=FSInputFile(file_path, filename=telegram_filename),
                title=f"{track.title}{bitrate_label}",
                performer=track.artist,
                duration=duration_to_use,
                caption=caption_text,
            )

            # Save file_id to cache for future requests
            if audio_msg and audio_msg.audio:
                await save_cached_file_id(track.artist, track.title, track.duration, audio_msg.audio.file_id)

        # Save to pending_tracks

        if callback.message.chat.type != "private":
            import json
            from dataclasses import asdict

            from app.db.database import get_db
            db = await get_db()
            try:
                # We need to save the search message ID to delete it later
                # We will keep the search message so users can download other tracks
                # Or we can link it. The PRD says "search message and original request are kept until vote passes".
                # Find original msg id from session
                orig_id = None
                row = await db.execute("SELECT original_msg_id FROM search_sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,))
                res = await row.fetchone()
                if res:
                    orig_id = res["original_msg_id"]

                # Add the column to pending_tracks if not exists
                try:
                    await db.execute("ALTER TABLE pending_tracks ADD COLUMN original_msg_id INTEGER")
                    await db.commit()
                except:
                    pass

                await db.execute(
                    "INSERT INTO pending_tracks (chat_id, audio_msg_id, search_msg_id, user_id, track_json, original_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (callback.message.chat.id, audio_msg.message_id, callback.message.message_id, user_id, json.dumps(asdict(track)), orig_id)
                )
                await db.commit()
            except Exception as dbe:
                logger.error(f"Failed to save pending track: {dbe}")
            finally:
                await db.close()

        await status.delete()

    except Exception as e:
        logger.error("Download failed: %s", e)
        await status.edit_text(f"❌ Ошибка скачивания: {e}")
    finally:
        if file_path and os.path.exists(file_path):
            cleanup_file(file_path)


@router.callback_query(F.data.startswith("back_to_list"))
async def cb_back_to_list(callback: CallbackQuery) -> None:
    """Return to search results page."""
    parts = callback.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 1
    
    user_id = callback.from_user.id
    tracks = get_cached_tracks(user_id)
    if not tracks:
        await callback.answer("Результаты устарели.")
        return

    # Extract artist from the first result to restore "Popular" button
    artist_name = tracks[0].artist if tracks else None
    
    msg_text = _format_results(tracks, page, per_page=10, total=len(tracks))
    await callback.message.edit_text(
        msg_text,
        reply_markup=search_results_kb(tracks, page, total=len(tracks), per_page=10, artist_name=artist_name),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("playlist:"))
async def cb_playlist(callback: CallbackQuery) -> None:
    """Handle playlist emoji buttons — route track to forum topic."""
    parts = callback.data.split(":")
    emoji = parts[1]
    track_idx = int(parts[2]) if len(parts) > 2 else 0

    if emoji == "new":
        # We need state to save track_idx

        # We will dispatch to FSM by modifying the handler signature slightly,
        # but aiogram automatically injects state if it's in the signature.
        # Since we can't change signature without breaking aiogram if we don't import FSMContext,
        # let's just save it into a generic DB table or cache for now to keep it simple and robust.

        # Simpler approach: save it in a small memory dict or DB
        from bot.handlers.forum import _new_playlist_cache
        _new_playlist_cache[callback.from_user.id] = track_idx

        await callback.message.answer("📁 Отправь мне эмодзи для нового плейлиста:")
        await callback.answer()
        return

    chat = callback.message.chat
    # Check if we're in a forum group
    if not getattr(chat, 'is_forum', False):
        await callback.answer("📂 Плейлисты работают в группах с топиками. Добавь бота в группу-форум!", show_alert=True)
        return

    await callback.answer(f"Отправляю в топик {emoji}...")
    # Delegate to forum handler logic
    from bot.handlers.forum import send_to_topic
    await send_to_topic(callback, emoji, track_idx)

@router.callback_query(F.data.startswith("select_track:"))
async def cb_select_track(callback: CallbackQuery) -> None:
    """Show details for a track before download."""
    parts = callback.data.split(":")
    track_idx = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 1

    user_id = callback.from_user.id
    tracks = get_cached_tracks(user_id)
    if not tracks or track_idx >= len(tracks):
        await callback.answer("❌ Сессия поиска истекла.")
        return
    track = tracks[track_idx]
    
    # Check if cached to show 'Fast Cache' vs 'Download'
    cached_file_id = await get_cached_file_id(track.artist, track.title, track.duration)
    is_cached = cached_file_id is not None

    # Check if admin to show 'Clear Cache' button
    is_admin = False
    async with get_db_ctx() as db:
        row = await db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,))
        res = await row.fetchone()
        if res and res["is_admin"] == 1:
            is_admin = True

    text = f"🎵 <b>{track.artist} – {track.title}</b>\n\n🕒 Длительность: {track.duration_str}\n📥 Источник: {track.source_name}"
    
    await callback.message.edit_text(
        text, 
        reply_markup=track_detail_kb(track_idx, source_name=track.source_name, is_admin=is_admin, is_cached=is_cached, page=page), 
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("clear_track_cache:"))
async def cb_clear_track_cache(callback: CallbackQuery) -> None:
    """Clear cache for a specific track."""
    parts = callback.data.split(":")
    track_idx = int(parts[1])
    user_id = callback.from_user.id
    
    tracks = get_cached_tracks(user_id)
    if not tracks or track_idx >= len(tracks):
        await callback.answer("❌ Сессия поиска истекла.")
        return
    track = tracks[track_idx]

    track_hash = generate_track_hash(track.artist, track.title, track.duration)

    async with get_db_ctx() as db:
        try:
            await db.execute("DELETE FROM cached_tracks WHERE artist_title_hash = ?", (track_hash,))
            await db.commit()
            await callback.answer("♻️ Кеш этого трека очищен. Теперь он скачается заново.", show_alert=True)
        except Exception as e:
            await callback.answer(f"❌ Ошибка очистки: {e}", show_alert=True)


@router.callback_query(F.data.startswith("popular_artist:"))
async def cb_popular_artist(callback: CallbackQuery) -> None:
    """Handle request for artist's popular tracks."""
    artist_name = callback.data.split(":", 1)[1]
    user_id = callback.from_user.id
    
    await callback.answer(f"🔍 Ищу топ-треки: {artist_name}")
    
    # Send intermediate status or just edit
    old_text = callback.message.text
    await callback.message.edit_text(f"{old_text}\n\n⏳ <b>Ищу лучшие хиты {artist_name}...</b>", parse_mode="HTML")

    from services import vk_music as vk_svc
    from services import youtube as yt_svc
    from services import spotify as sp_svc
    import asyncio
    from app.db.models import TrackInfo
    from bot.handlers.search import _format_results, _search_cache
    from services.vk_captcha import captcha_manager

    try:
        c_handler = captcha_manager.get_captcha_handler(
            callback.bot, 
            callback.message.chat.id, 
            callback.from_user.id
        )
        # Search for artist name directly
        yt_task = yt_svc.search(artist_name, count=20)
        vk_task = vk_svc.search(artist_name, count=20, captcha_handler=c_handler)
        sp_task = sp_svc.search(artist_name, count=20)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)
        
        all_tracks: list[TrackInfo] = []
        for r in results:
            if isinstance(r, list):
                all_tracks.extend(r)
        
        if not all_tracks:
            await callback.message.edit_text(f"😔 Не удалось найти популярные треки {artist_name}")
            return

        # Update cache
        _search_cache[user_id] = all_tracks
        
        # Format and update message
        text = _format_results(all_tracks, 1, 10, len(all_tracks))
        kb = search_results_kb(all_tracks, 1, len(all_tracks), artist_name=artist_name)
        
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Popular search failed: {e}")
        await callback.message.edit_text(f"❌ Ошибка при поиске хитов {artist_name}")
