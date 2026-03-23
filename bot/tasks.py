import asyncio
import logging
from datetime import datetime, timedelta
from app.db.database import get_db
from bot.handlers.admin import get_global_settings

logger = logging.getLogger(__name__)

async def cleanup_loop(bot):
    """Periodically check for old pending tracks, and process scheduled track routing."""
    from bot.handlers.forum import _get_topic_map, _save_topic
    import time
    import json
    from app.db.models import TrackInfo


    while True:
        try:
            from app.db.database import get_db_ctx
            async with get_db_ctx() as db:
                # Fetch one pending track
                row = await db.execute("SELECT * FROM playlist_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1")
                task = await row.fetchone()

                if not task:
                    pass # Handled after context
                else:
                    task_id = task["id"]
                    chat_id = task["chat_id"]
                    user_id = task["user_id"]
                    orig_msg_id = task["original_msg_id"]
                    track_data = json.loads(task["track_json"])
                    track = TrackInfo(**track_data)

                    # Mark as processing
                    await db.execute("UPDATE playlist_queue SET status = 'processing' WHERE id = ?", (task_id,))
                    await db.commit()

            if not task:
                await asyncio.sleep(10)
                continue

            # Anti-bot delay and logic outside db context to not hold connection
            cached_file_id = await get_cached_file_id(track.artist, track.title)

            audio_msg = None
            caption_text = f"{track.source_icon} {track.artist} – {track.title}\n👤 #{user_id} (Плейлист)"

            if cached_file_id:
                logger.info(f"Playlist Queue: Using cached file ID for {track.artist} - {track.title}")
                try:
                    audio_msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cached_file_id,
                        caption=caption_text,
                        message_thread_id=None
                    )
                except Exception as e:
                    logger.warning(f"Playlist Queue: Failed to send cached audio: {e}")
                    cached_file_id = None

            if not cached_file_id:
                delay = random.uniform(30.0, 100.0)
                logger.info(f"Playlist Queue: Sleep for {delay:.1f}s before downloading {track.title}...")
                await asyncio.sleep(delay)

                batch_count += 1
                if batch_count >= random.randint(20, 30):
                    pause_time = random.uniform(300.0, 600.0)
                    logger.info(f"Playlist Queue: Batch limit reached. Taking a long pause for {pause_time:.1f}s...")
                    await asyncio.sleep(pause_time)
                    batch_count = 0

                file_path = await download_track(track, "mp3_320")

                if not file_path or not os.path.exists(file_path):
                    logger.warning(f"Playlist Queue: VK download failed for {track.title}, falling back to YouTube...")
                    from services import youtube as yt_svc
                    yt_results = await yt_svc.search(f"{track.artist} {track.title}", count=1)
                    if yt_results:
                        track = yt_results[0]
                        file_path = await download_track(track, "mp3_320")

                if file_path and os.path.exists(file_path):
                    try:
                        audio_msg = await bot.send_audio(
                            chat_id=chat_id,
                            audio=FSInputFile(file_path),
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration,
                            caption=caption_text,
                            message_thread_id=None
                        )
                        if audio_msg and audio_msg.audio:
                            await save_cached_file_id(track.artist, track.title, audio_msg.audio.file_id)
                    except Exception as send_e:
                        logger.error(f"Playlist Queue: Failed to send downloaded track: {send_e}")
                    finally:
                        cleanup_file(file_path)

            async with get_db_ctx() as db:
                if audio_msg:
                    await db.execute(
                        "INSERT INTO pending_tracks (chat_id, audio_msg_id, search_msg_id, user_id, track_json, original_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (chat_id, audio_msg.message_id, 0, user_id, json.dumps(track_data), orig_msg_id)
                    )
                    await db.execute("UPDATE playlist_queue SET status = 'done' WHERE id = ?", (task_id,))
                else:
                    await db.execute("UPDATE playlist_queue SET status = 'failed' WHERE id = ?", (task_id,))
                await db.commit()

        except Exception as e:
            logger.error(f"Error in playlist_queue_loop: {e}")
            await asyncio.sleep(10)
            user_id = task["user_id"]
            orig_msg_id = task["original_msg_id"]
            track_data = json.loads(task["track_json"])
            track = TrackInfo(**track_data)

            # Mark as processing
            await db.execute("UPDATE playlist_queue SET status = 'processing' WHERE id = ?", (task_id,))
            await db.commit()

            # Anti-bot delay if we are actually downloading (not cached)
            # but let's check cache first
            cached_file_id = await get_cached_file_id(track.artist, track.title)

            audio_msg = None

            caption_text = f"{track.source_icon} {track.artist} – {track.title}\\n👤 #{user_id} (Плейлист)"

            if cached_file_id:
                logger.info(f"Playlist Queue: Using cached file ID for {track.artist} - {track.title}")
                try:
                    audio_msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cached_file_id,
                        caption=caption_text,
                        message_thread_id=None # Send to general or DM
                    )
                except Exception as e:
                    logger.warning(f"Playlist Queue: Failed to send cached audio: {e}")
                    cached_file_id = None

            if not cached_file_id:
                # Anti-bot logic: Random delay
                delay = random.uniform(30.0, 100.0)
                logger.info(f"Playlist Queue: Sleep for {delay:.1f}s before downloading {track.title}...")
                await asyncio.sleep(delay)

                # Batch processing pause
                batch_count += 1
                if batch_count >= random.randint(20, 30):
                    pause_time = random.uniform(300.0, 600.0) # 5-10 minutes
                    logger.info(f"Playlist Queue: Batch limit reached. Taking a long pause for {pause_time:.1f}s...")
                    await asyncio.sleep(pause_time)
                    batch_count = 0

                # Try downloading from VK (which now adds to My Audios first)
                file_path = await download_track(track, "mp3_320")

                # Fallback to YouTube if VK fails
                if not file_path or not os.path.exists(file_path):
                    logger.warning(f"Playlist Queue: VK download failed for {track.title}, falling back to YouTube...")
                    # Search YT
                    from services import youtube as yt_svc
                    yt_results = await yt_svc.search(f"{track.artist} {track.title}", count=1)
                    if yt_results:
                        track = yt_results[0]
                        file_path = await download_track(track, "mp3_320")

                if file_path and os.path.exists(file_path):
                    try:
                        audio_msg = await bot.send_audio(
                            chat_id=chat_id,
                            audio=FSInputFile(file_path),
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration,
                            caption=caption_text,
                            message_thread_id=None
                        )
                        if audio_msg and audio_msg.audio:
                            await save_cached_file_id(track.artist, track.title, audio_msg.audio.file_id)
                    except Exception as send_e:
                        logger.error(f"Playlist Queue: Failed to send downloaded track: {send_e}")
                    finally:
                        cleanup_file(file_path)

            # If successfully sent (either cached or downloaded)
            if audio_msg:
                # Save to pending_tracks so voting works
                await db.execute(
                    "INSERT INTO pending_tracks (chat_id, audio_msg_id, search_msg_id, user_id, track_json, original_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, audio_msg.message_id, 0, user_id, json.dumps(track_data), orig_msg_id)
                )
                await db.execute("UPDATE playlist_queue SET status = 'done' WHERE id = ?", (task_id,))
            else:
                await db.execute("UPDATE playlist_queue SET status = 'failed' WHERE id = ?", (task_id,))

            await db.commit()
            await db.close()

        except Exception as e:
            logger.error(f"Error in playlist_queue_loop: {e}")
            await asyncio.sleep(10)




async def playlist_queue_loop(bot):
    """Process pending tracks in the playlist_queue with anti-bot delays."""
    import time
    import json
    import random
    from app.db.database import get_db, get_db_ctx
    from app.db.models import TrackInfo
    from services.downloader import download_track, cleanup_file
    from services.cache import get_cached_file_id, save_cached_file_id
    from aiogram.types import FSInputFile
    import os

    batch_count = 0

    while True:
        try:
            async with get_db_ctx() as db:
                row = await db.execute("SELECT * FROM playlist_queue WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1")
                task = await row.fetchone()

                if not task:
                    pass # Will sleep below
                else:
                    task_id = task["id"]
                    chat_id = task["chat_id"]
                    user_id = task["user_id"]
                    orig_msg_id = task["original_msg_id"]
                    track_data = json.loads(task["track_json"])
                    track = TrackInfo(**track_data)

                    await db.execute("UPDATE playlist_queue SET status = 'processing' WHERE id = ?", (task_id,))
                    await db.commit()

            if not task:
                await asyncio.sleep(10)
                continue

            cached_file_id = await get_cached_file_id(track.artist, track.title)
            audio_msg = None
            caption_text = f"{track.source_icon} {track.artist} – {track.title}\n👤 #{user_id} (Плейлист)"

            if cached_file_id:
                logger.info(f"Playlist Queue: Using cached file ID for {track.artist} - {track.title}")
                try:
                    audio_msg = await bot.send_audio(
                        chat_id=chat_id,
                        audio=cached_file_id,
                        caption=caption_text,
                        message_thread_id=None
                    )
                except Exception as e:
                    logger.warning(f"Playlist Queue: Failed to send cached audio: {e}")
                    cached_file_id = None

            if not cached_file_id:
                delay = random.uniform(30.0, 100.0)
                logger.info(f"Playlist Queue: Sleep for {delay:.1f}s before downloading {track.title}...")
                await asyncio.sleep(delay)

                batch_count += 1
                if batch_count >= random.randint(20, 30):
                    pause_time = random.uniform(300.0, 600.0)
                    logger.info(f"Playlist Queue: Batch limit reached. Taking a long pause for {pause_time:.1f}s...")
                    await asyncio.sleep(pause_time)
                    batch_count = 0

                file_path = await download_track(track, "mp3_320")

                if not file_path or not os.path.exists(file_path):
                    logger.warning(f"Playlist Queue: VK download failed for {track.title}, falling back to YouTube...")
                    from services import youtube as yt_svc
                    yt_results = await yt_svc.search(f"{track.artist} {track.title}", count=1)
                    if yt_results:
                        track = yt_results[0]
                        file_path = await download_track(track, "mp3_320")

                if file_path and os.path.exists(file_path):
                    try:
                        audio_msg = await bot.send_audio(
                            chat_id=chat_id,
                            audio=FSInputFile(file_path),
                            title=track.title,
                            performer=track.artist,
                            duration=track.duration,
                            caption=caption_text,
                            message_thread_id=None
                        )
                        if audio_msg and audio_msg.audio:
                            await save_cached_file_id(track.artist, track.title, audio_msg.audio.file_id)
                    except Exception as send_e:
                        logger.error(f"Playlist Queue: Failed to send downloaded track: {send_e}")
                    finally:
                        cleanup_file(file_path)

            async with get_db_ctx() as db:
                if audio_msg:
                    await db.execute(
                        "INSERT INTO pending_tracks (chat_id, audio_msg_id, search_msg_id, user_id, track_json, original_msg_id) VALUES (?, ?, ?, ?, ?, ?)",
                        (chat_id, audio_msg.message_id, 0, user_id, json.dumps(track_data), orig_msg_id)
                    )
                    await db.execute("UPDATE playlist_queue SET status = 'done' WHERE id = ?", (task_id,))
                else:
                    await db.execute("UPDATE playlist_queue SET status = 'failed' WHERE id = ?", (task_id,))
                await db.commit()

        except Exception as e:
            logger.error(f"Error in playlist_queue_loop: {e}")
            await asyncio.sleep(10)
