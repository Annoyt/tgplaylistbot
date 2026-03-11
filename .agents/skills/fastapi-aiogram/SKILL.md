---
name: fastapi-aiogram
description: Contains expertise on the architectural setup of the tgplaylistbot dual-process app (FastAPI web server + aiogram Telegram bot).
---

# FastAPI + Aiogram Agent

You are working on `tgplaylistbot`, a monolith that uniquely runs both a standard REST API backend (FastAPI) and an async Telegram Bot (Aiogram 3.x) inside a single process.

## Project Structure
- Entrypoint: `run.py` - Uses `asyncio.gather(run_web(), run_bot())` to start Uvicorn programmatically and start aiogram polling concurrently.
- Web Layer: `app/main.py` orchestrates FastAPI routers (`weather`, `admin`, `auth`).
- Bot Layer: `bot/handlers/` contains feature-specific logical divisions:
  - `callbacks.py`: UI navigation and file downloads.
  - `forum.py`: Advanced Forum Topics / Playlists features & Reactions logic.
  - `recognize.py`: Shazam-like features operating on varied media types.
  - `search.py`: Unified external searching.

## Rules
1. DO NOT define long-running blocking operations anywhere, since the entire system operates in a single async event loop constraint.
2. If modifying or adding handlers, make sure they are properly imported and registered in `run.py` (order matters: wildcard/text handlers should load last).
3. Do not assume web requests and Telegram updates share identical security mechanisms. The Web app relies on JWT in httpOnly cookies, the bot relies on user IDs from Telegram.
