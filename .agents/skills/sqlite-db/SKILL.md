---
name: sqlite-db
description: Expertise on the `tgplaylistbot` database schema, focusing on the zero-storage track approach and forum topics mappings.
---

# SQLite Database Manager

You are interacting with the `tgplaylistbot` database layer (implemented via `aiosqlite`). 

## Key Patterns
- **Zero Server Storage**: Tracks are downloaded, sent to Telegram directly, and deleted off the server (with `tempfile`/`os.remove` or custom cleanup from `downloader.py`). There is NO `tracks` or `playlists_tracks` relational tables saved.
- **Forum Topics Mappings**: 
  - Stored in `forum_topics` table (maps a group's `chat_id` and emoji to a specific `topic_id`).
  - Stored dynamically via `_save_topic()`.
- **User Settings**: Stored per user_id in `user_settings` relating to download qualities and API sources.

## Important Schema Information
- The `init_db()` in `app/db/database.py` manages tables. Any schema migrations or expansions MUST occur there within `CREATE TABLE IF NOT EXISTS` or manual `ALTER TABLE`.
- We use strict PRAGMA modes and standard relationships.

DO NOT construct `DROP TABLE` statements on production data unless explicitly tasked to reset the bot state.
