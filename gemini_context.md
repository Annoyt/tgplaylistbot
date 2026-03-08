# Контекстный пакет для Gemini (Context Manager)

> Этот файл создан Claude (CTO) для передачи Gemini.
> Gemini использует его для подготовки контекстного пакета для Kimi.

## Задача

Kimi реализует полную платформу по PRD из `kimi_prompt.txt`.

## Файлы, которые Kimi ДОЛЖЕН прочитать перед началом работы

### Уже существующие (скелет от CTO):
1. `config.py` — pydantic-settings, все env vars
2. `app/db/database.py` — SQLite init, таблицы
3. `app/db/models.py` — TrackInfo, UserSettings, ForumTopic, RecognizedTrack
4. `app/auth/service.py` — JWT + bcrypt
5. `app/auth/dependencies.py` — get_current_user
6. `app/auth/router.py` — /login, /logout
7. `app/weather/service.py` — OpenWeatherMap клиент
8. `app/weather/router.py` — REST API
9. `app/admin/router.py` — admin CRUD
10. `services/recognizer.py` — shazamio + AcoustID
11. `services/youtube.py` — yt-dlp search/download
12. `services/vk_music.py` — VK audio
13. `services/spotify.py` — spotipy metadata
14. `services/downloader.py` — unified download + cleanup
15. `requirements.txt` — зависимости
16. `.env.example` — шаблон переменных

### Файлы, которые Kimi ДОЛЖЕН создать:
1. `app/main.py` — FastAPI app, mount routers, startup events
2. `app/templates/base.html` — базовый layout
3. `app/templates/index.html` — Weather Dashboard (два блока локации)
4. `app/templates/login.html` — логин-форма
5. `app/templates/admin/dashboard.html` — обзор ботов
6. `app/templates/admin/musicbot.html` — настройки MusicBot
7. `app/templates/admin/weatherbot.html` — placeholder
8. `app/static/css/style.css` — тёмная тема, glassmorphism
9. `app/static/js/weather.js` — fetch API, geolocation, карточки
10. `app/static/js/admin.js` — формы администрирования
11. `bot/handlers/start.py` — /start, /help
12. `bot/handlers/search.py` — текстовый поиск, 10 на страницу
13. `bot/handlers/recognize.py` — voice, video, links
14. `bot/handlers/callbacks.py` — download, pagination, filters, playlist emoji
15. `bot/handlers/forum.py` — auto-create topics, 👎 voting, /topics
16. `bot/handlers/settings.py` — /settings inline menu
17. `bot/keyboards/inline.py` — все keyboard builders
18. `run.py` — asyncio.gather(web, bot)
19. `Dockerfile`
20. `docker-compose.yml`

## Зависимости между файлами

```
config.py ← используется ВЕЗДЕ
app/db/models.py ← используется в services/* и bot/handlers/*
services/* ← используются в bot/handlers/*
bot/keyboards/inline.py ← используется в bot/handlers/*
app/main.py ← импортирует все app/ routers
run.py ← импортирует app/main.py и bot/
```

## Подсказки для Kimi

- aiogram 3: используй Router(), не Dispatcher напрямую для handlers
- Forum topics API: `bot.create_forum_topic()`, `message.message_thread_id`
- Reactions: `MessageReactionUpdated` handler
- yt-dlp запускать через asyncio.create_subprocess_exec
- Все блокирующие вызовы (vk_api, spotipy) → asyncio.run_in_executor
