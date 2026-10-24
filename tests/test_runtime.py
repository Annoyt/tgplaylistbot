"""config.Settings, DB schema/migrations, run.main shutdown, background loops."""

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from app.db.database import get_db_ctx, init_db
from config import Settings, settings


# ── config ─────────────────────────────────────────────────────────────────

class TestSettings:
    def test_db_path(self):
        s = Settings(database_url="sqlite+aiosqlite:////app/data/bot.db")
        assert s.db_path == Path("/app/data/bot.db")
        assert s.cookies_path == Path("/app/data/cookies.txt")

    def test_cookie_args(self, tmp_settings):
        assert tmp_settings.cookie_args() == []
        tmp_settings.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.cookies_path.write_text("x")
        assert tmp_settings.cookie_args() == ["--cookies", str(tmp_settings.cookies_path)]

    @pytest.mark.parametrize("base,local,limit_mb", [("", False, 50), ("  ", False, 50), ("http://api:8081", True, 2000)])
    def test_send_limit(self, base, local, limit_mb):
        s = Settings(telegram_api_base_url=base)
        assert s.local_bot_api is local and s.send_limit_bytes == limit_mb * 1024 * 1024

    def test_download_path_is_created(self, tmp_path):
        s = Settings(download_dir=str(tmp_path / "a" / "b"))
        assert s.download_path.is_dir()


# ── schema ─────────────────────────────────────────────────────────────────

async def columns(table):
    async with get_db_ctx() as db:
        return {r[1] for r in await (await db.execute(f"PRAGMA table_info({table})")).fetchall()}


class TestSchema:
    async def test_idempotent_and_complete(self, db):
        await init_db()
        assert {"route_at", "route_emoji", "original_msg_id"} <= await columns("pending_tracks")
        assert "original_msg_id" in await columns("search_sessions")
        assert "nickname" in await columns("users")

    async def test_migrates_legacy_tables_column_by_column(self, tmp_settings):
        path = tmp_settings.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(path) as conn:
            # legacy: route_at already there, route_emoji / original_msg_id missing
            await conn.execute("CREATE TABLE pending_tracks (id INTEGER PRIMARY KEY, chat_id INTEGER, audio_msg_id INTEGER,"
                               " search_msg_id INTEGER, user_id INTEGER, track_json TEXT, route_at INTEGER DEFAULT 0)")
            await conn.commit()
        await init_db()
        assert {"route_at", "route_emoji", "original_msg_id"} <= await columns("pending_tracks")

    async def test_get_db_ctx_closes_on_error(self, db):
        with pytest.raises(RuntimeError):
            async with get_db_ctx() as conn:
                raise RuntimeError("boom")
        with pytest.raises(ValueError):
            await conn.execute("SELECT 1")   # closed connection


# ── run.main shutdown ──────────────────────────────────────────────────────

@pytest.fixture
def run_mod(monkeypatch):
    import run
    monkeypatch.setattr("app.db.database.init_db", AsyncMock())
    monkeypatch.setattr(run, "_web_server", None)
    return run


def fake_web(run, stopped):
    async def web():
        server = SimpleNamespace(should_exit=False)
        run._web_server = server
        while not server.should_exit:
            await asyncio.sleep(0.005)
        stopped.append("web-graceful")
    return web


class TestShutdown:
    async def test_bot_stopping_stops_web_gracefully(self, run_mod, monkeypatch):
        stopped = []
        monkeypatch.setattr(run_mod.settings, "telegram_bot_token", "x")
        monkeypatch.setattr(run_mod, "run_web", fake_web(run_mod, stopped))

        async def bot():
            await asyncio.sleep(0.02)        # aiogram caught SIGTERM and returned
        monkeypatch.setattr(run_mod, "run_bot", bot)
        await asyncio.wait_for(run_mod.main(), timeout=2)
        assert stopped == ["web-graceful"]

    async def test_web_stopping_cancels_bot(self, run_mod, monkeypatch):
        cancelled = []
        monkeypatch.setattr(run_mod.settings, "telegram_bot_token", "x")

        async def web():
            await asyncio.sleep(0.02)        # uvicorn caught SIGTERM and returned

        async def bot():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        monkeypatch.setattr(run_mod, "run_web", web)
        monkeypatch.setattr(run_mod, "run_bot", bot)
        await asyncio.wait_for(run_mod.main(), timeout=2)
        assert cancelled == [True]

    async def test_bot_crash_is_raised_for_a_restart(self, run_mod, monkeypatch):
        monkeypatch.setattr(run_mod.settings, "telegram_bot_token", "x")
        monkeypatch.setattr(run_mod, "run_web", fake_web(run_mod, []))

        async def bot():
            raise RuntimeError("bad token")
        monkeypatch.setattr(run_mod, "run_bot", bot)
        with pytest.raises(RuntimeError, match="bad token"):
            await asyncio.wait_for(run_mod.main(), timeout=2)

    async def test_without_token_only_web_runs(self, run_mod, monkeypatch):
        monkeypatch.setattr(run_mod.settings, "telegram_bot_token", "")
        bot = AsyncMock()
        monkeypatch.setattr(run_mod, "run_bot", bot)
        monkeypatch.setattr(run_mod, "run_web", AsyncMock())
        await asyncio.wait_for(run_mod.main(), timeout=2)
        bot.assert_not_called()

    async def test_stuck_web_is_cancelled_after_grace(self, run_mod, monkeypatch):
        monkeypatch.setattr(run_mod.settings, "telegram_bot_token", "x")
        cancelled = []

        async def web():
            run_mod._web_server = SimpleNamespace(should_exit=False)   # ignores should_exit
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def bot():
            await asyncio.sleep(0.01)
        monkeypatch.setattr(run_mod, "run_web", web)
        monkeypatch.setattr(run_mod, "run_bot", bot)
        real_wait = asyncio.wait
        monkeypatch.setattr(run_mod.asyncio, "wait",
                            lambda fs, timeout=None, **kw: real_wait(fs, timeout=0.05 if timeout else None, **kw))
        await asyncio.wait_for(run_mod.main(), timeout=2)
        assert cancelled == [True]


# ── background loops ───────────────────────────────────────────────────────

class StopLoop(BaseException):
    """Escapes the loops' `except Exception` so a test can end them."""


def sleep_stopping_on(*stop_on):
    async def fake_sleep(sec):
        if sec in stop_on:
            raise StopLoop
    return fake_sleep


async def queue_row(status, track_json):
    async with get_db_ctx() as db:
        cur = await db.execute(
            "INSERT INTO playlist_queue (chat_id, user_id, original_msg_id, track_json, status) VALUES (5, 7, 9, ?, ?)",
            (track_json, status))
        await db.commit()
        return cur.lastrowid


async def queue_status(row_id):
    async with get_db_ctx() as db:
        return (await (await db.execute("SELECT status FROM playlist_queue WHERE id = ?", (row_id,))).fetchone())[0]


class TestPlaylistQueue:
    @pytest.fixture
    def tasks(self, monkeypatch):
        from bot import tasks
        monkeypatch.setattr(tasks.asyncio, "sleep", sleep_stopping_on(10))
        monkeypatch.setattr("random.uniform", lambda a, b: 0.0)
        return tasks

    async def test_unreadable_row_is_failed_not_retried_forever(self, db, tasks):
        row = await queue_row("pending", "{not json")
        with pytest.raises(StopLoop):
            await tasks.playlist_queue_loop(SimpleNamespace())
        assert await queue_status(row) == "failed"

    async def test_interrupted_row_is_requeued_on_start(self, db, tasks, monkeypatch, make_track):
        row = await queue_row("processing", json.dumps(asdict(make_track(title="Song", artist="A", duration=120))))
        seen = {}

        async def cached(artist, title, duration=0):
            seen["args"] = (artist, title, duration)
            return "file-id"
        monkeypatch.setattr("services.cache.get_cached_file_id", cached)
        bot = SimpleNamespace(send_audio=AsyncMock(return_value=SimpleNamespace(message_id=55, audio=None)))
        with pytest.raises(StopLoop):
            await tasks.playlist_queue_loop(bot)
        assert await queue_status(row) == "done"
        assert seen["args"] == ("A", "Song", 120)                    # duration passed to the cache lookup
        assert bot.send_audio.await_args.kwargs["audio"] == "file-id"
        async with get_db_ctx() as conn:
            pending = await (await conn.execute("SELECT chat_id, audio_msg_id, original_msg_id FROM pending_tracks")).fetchall()
        assert [tuple(p) for p in pending] == [(5, 55, 9)]

    async def test_download_failure_marks_failed(self, db, tasks, monkeypatch, make_track):
        row = await queue_row("pending", json.dumps(asdict(make_track(source="vk"))))
        monkeypatch.setattr("services.cache.get_cached_file_id", AsyncMock(return_value=None))
        monkeypatch.setattr("services.downloader.download_track", AsyncMock(return_value=None))
        monkeypatch.setattr("services.youtube.search", AsyncMock(return_value=[]))
        with pytest.raises(StopLoop):
            await tasks.playlist_queue_loop(SimpleNamespace(send_audio=AsyncMock()))
        assert await queue_status(row) == "failed"

    async def test_unexpected_error_does_not_leave_processing(self, db, tasks, monkeypatch, make_track):
        row = await queue_row("pending", json.dumps(asdict(make_track())))
        monkeypatch.setattr("services.cache.get_cached_file_id", AsyncMock(side_effect=RuntimeError("boom")))
        with pytest.raises(StopLoop):
            await tasks.playlist_queue_loop(SimpleNamespace())
        assert await queue_status(row) == "failed"

    async def test_sent_track_is_cached_with_duration(self, db, tasks, monkeypatch, make_track, tmp_path):
        row = await queue_row("pending", json.dumps(asdict(make_track(title="S", artist="A", duration=99))))
        f = tmp_path / "s.mp3"
        f.write_bytes(b"x")
        monkeypatch.setattr("services.cache.get_cached_file_id", AsyncMock(return_value=None))
        save = AsyncMock()
        monkeypatch.setattr("services.cache.save_cached_file_id", save)
        monkeypatch.setattr("services.downloader.download_track", AsyncMock(return_value=str(f)))
        sent = SimpleNamespace(message_id=1, audio=SimpleNamespace(file_id="new-fid"))
        with pytest.raises(StopLoop):
            await tasks.playlist_queue_loop(SimpleNamespace(send_audio=AsyncMock(return_value=sent)))
        save.assert_awaited_once_with("A", "S", 99, "new-fid")
        assert await queue_status(row) == "done" and not f.exists()


class TestOtherLoops:
    async def test_disk_cleanup_loop_runs_and_survives_errors(self, monkeypatch):
        from bot import tasks
        calls = []

        def cleanup(older_than_sec):
            calls.append(older_than_sec)
            raise OSError("disk")
        monkeypatch.setattr("services.downloader.cleanup_download_dir", cleanup)
        monkeypatch.setattr(tasks.asyncio, "sleep", sleep_stopping_on(600))
        with pytest.raises(StopLoop):
            await tasks.disk_cleanup_loop()
        assert calls == [3600]

    async def test_ytdlp_update_loop(self, monkeypatch):
        from bot import tasks
        slept = []

        async def fake_sleep(sec):
            slept.append(sec)
            if len(slept) == 2:
                raise StopLoop
        upd = AsyncMock(side_effect=RuntimeError("pip down"))
        monkeypatch.setattr("services.youtube.update_ytdlp", upd)
        monkeypatch.setattr(tasks.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(settings, "ytdlp_update_hours", 12)
        with pytest.raises(StopLoop):
            await tasks.ytdlp_update_loop()
        assert slept == [30, 12 * 3600] and upd.await_count == 1
