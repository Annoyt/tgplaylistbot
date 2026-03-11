# tgplaylistbot (VKMusicBot + Weather Platform)

Welcome to the `tgplaylistbot` project! 

## Architecture
- **Web App**: FastAPI (serves the Weather dashboard and Admin panel).
- **Telegram Bot**: `aiogram` 3.x.
- Both components run in a single concurrent asyncio event loop orchestrated by `run.py`.

## Core Conventions
1. **Zero Server Storage**: Tracks are downloaded to `/tmp/musicbot`, sent to Telegram, and immediately deleted. We do not persist audio files or catalog them in a database.
2. **Forum Topics**: Playlists are managed using Telegram Forum Topics. The bot maps topics via emojis (e.g., ❤️, 🔥).
3. **Database**: `aiosqlite` is used. We do NOT use heavy ORMs right now. See `app/db/` for schema definitions.

## Agent Guidelines
- Check `.agents/skills/` if you need detailed expertise on security audits, deployment, or our FastAPI/aiogram integration.
- If you change the frontend (`app/templates/` or `app/static/`), keep the "dark theme / glassmorphism" aesthetics intact.
- If you deploy, run `/deploy` command via Gemini CLI or see the `.github/workflows/deploy.yml`.
