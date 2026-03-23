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
            s = await get_global_settings()
            db = await get_db()

            # 1. Process routing (tracks that hit threshold and passed the wait time)
            now = int(time.time())
            try:
                row = await db.execute("SELECT * FROM pending_tracks WHERE route_at > 0 AND route_at <= ?", (now,))
                ready_tracks = await row.fetchall()

                for pt in ready_tracks:
                    try:
                        winning_emoji = pt["route_emoji"]
                        chat_id = pt["chat_id"]

                        if winning_emoji == "👎":
                            try:
                                await bot.delete_message(chat_id, pt["audio_msg_id"])
                            except: pass
                        else:
                            # Route track
                            track_data = json.loads(pt["track_json"])
                            track = TrackInfo(**track_data)

                            topic_map = await _get_topic_map(chat_id)
                            topic_id = topic_map.get(winning_emoji)

                            if not topic_id:
                                # Create topic
                                name = track.artist[:15]
                                try:
                                    topic = await bot.create_forum_topic(chat_id=chat_id, name=f"{winning_emoji} {name}")
                                    topic_id = topic.message_thread_id
                                    await _save_topic(chat_id, topic_id, winning_emoji, name)
                                except Exception as e:
                                    logger.error(f"Failed to create topic: {e}")
                                    raise  # Stop routing this track but delete it from pending so we don't spam

                            new_msg_id = None
                            try:
                                if s.forward_mode == "forward":
                                    msg = await bot.forward_message(chat_id=chat_id, from_chat_id=chat_id, message_id=pt["audio_msg_id"], message_thread_id=topic_id)
                                    new_msg_id = msg.message_id
                                else:
                                    msg = await bot.copy_message(chat_id=chat_id, from_chat_id=chat_id, message_id=pt["audio_msg_id"], message_thread_id=topic_id)
                                    new_msg_id = msg.message_id

                                # Save to topic_messages to track it for downvotes and auto-cleanup
                                if new_msg_id:
                                    await db.execute("INSERT INTO topic_messages (chat_id, topic_id, message_id) VALUES (?, ?, ?)", (chat_id, topic_id, new_msg_id))
                            except Exception as fwd_err:
                                logger.error(f"Failed to forward/copy message {pt['audio_msg_id']}: {fwd_err}")
                                raise

                            # Delete original audio message
                            try:
                                await bot.delete_message(chat_id, pt["audio_msg_id"])
                            except: pass

                    except Exception as route_exec_err:
                        logger.error(f"Error executing route for track {pt['id']}: {route_exec_err}")
                    finally:
                        # Ensure cleanup always happens regardless of routing success
                        # Cleanup search menu message
                        if pt["search_msg_id"] and pt["search_msg_id"] > 0:
                            try:
                                await bot.delete_message(pt["chat_id"], pt["search_msg_id"])
                            except: pass

                        # Cleanup original text/link message
                        if "original_msg_id" in pt.keys() and pt["original_msg_id"] and pt["original_msg_id"] > 0:
                            try:
                                await bot.delete_message(pt["chat_id"], pt["original_msg_id"])
                            except: pass

                        # Finally remove from pending to prevent poison loop
                        try:
                            await db.execute("DELETE FROM pending_tracks WHERE id = ?", (pt["id"],))
                            await db.commit()
                        except: pass
            except Exception as routing_err:
                logger.error(f"Routing error: {routing_err}")

            # 2. Cleanup old spam (7 days)
            query = f"SELECT * FROM pending_tracks WHERE datetime(created_at) < datetime('now', '-{s.msg_ttl_days} days')"

            try:
                row = await db.execute(query)
                expired_tracks = await row.fetchall()

                for pt in expired_tracks:
                    try:
                        await bot.delete_message(pt["chat_id"], pt["audio_msg_id"])
                    except: pass
                    if pt["search_msg_id"] and pt["search_msg_id"] > 0:
                        try:
                            await bot.delete_message(pt["chat_id"], pt["search_msg_id"])
                        except: pass

                    if "original_msg_id" in pt.keys() and pt["original_msg_id"] and pt["original_msg_id"] > 0:
                        try:
                            await bot.delete_message(pt["chat_id"], pt["original_msg_id"])
                        except: pass

                    await db.execute("DELETE FROM pending_tracks WHERE id = ?", (pt["id"],))
                    await db.commit()
            except Exception as cleanup_err:
                logger.error(f"Cleanup error: {cleanup_err}")

        except Exception as e:
            logger.error(f"Error in task loop: {e}")
        finally:
            try:
                await db.close()
            except: pass

        # Check every 5 seconds for fast routing
        await asyncio.sleep(5)




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
                    pass
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
