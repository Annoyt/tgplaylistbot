"""Database layer: async SQLite connection and table management."""

import aiosqlite
from contextlib import asynccontextmanager

from config import settings


async def get_db() -> aiosqlite.Connection:
    """Return an async database connection."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


@asynccontextmanager
async def get_db_ctx():
    """Async context manager for database connections."""
    db = await get_db()
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    """Create tables if they do not exist."""
    async with get_db_ctx() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY,
                username      TEXT,
                is_admin      INTEGER DEFAULT 0,
                created_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS linked_groups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL,
                chat_id         INTEGER NOT NULL UNIQUE,
                chat_title      TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS forum_topics (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id         INTEGER NOT NULL,
                topic_id        INTEGER NOT NULL,
                emoji           TEXT    NOT NULL,
                name            TEXT    NOT NULL,
                is_search       INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT (datetime('now')),
                UNIQUE(chat_id, topic_id)
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                user_id             INTEGER PRIMARY KEY,
                default_quality     TEXT    DEFAULT 'mp3_320',
                results_per_page    INTEGER DEFAULT 10,
                platform_priority   TEXT    DEFAULT 'youtube,vk,spotify',
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            """
        )
        await db.commit()
