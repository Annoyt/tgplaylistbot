"""Admin panel router — protected by JWT auth."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from bot.handlers.admin import get_global_settings, update_global_setting
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user
from app.db.database import get_db_ctx
from app.auth.service import hash_password

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: dict = Depends(get_current_user)):
    g_settings = await get_global_settings()
    return templates.TemplateResponse("admin/dashboard.html", {"request": request, "user": user, "settings": g_settings})

@router.post("/settings")
async def save_settings(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    vote_threshold_pct = form.get("vote_threshold_pct")
    vote_interval_sec = form.get("vote_interval_sec")
    download_delay_sec = form.get("download_delay_sec")
    msg_ttl_days = form.get("msg_ttl_days")
    forward_mode = form.get("forward_mode")

    await update_global_setting("vote_threshold_pct", str(vote_threshold_pct))
    await update_global_setting("vote_interval_sec", str(vote_interval_sec))
    await update_global_setting("download_delay_sec", str(download_delay_sec))
    await update_global_setting("msg_ttl_days", str(msg_ttl_days))
    await update_global_setting("forward_mode", str(forward_mode))

    return RedirectResponse(url="/admin", status_code=303)


@router.post("/password")
async def change_password(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    new_pwd = form.get("new_password", "")
    confirm_pwd = form.get("confirm_password", "")

    if not new_pwd or new_pwd != confirm_pwd or len(new_pwd) < 6:
        return RedirectResponse(url="/admin/?pwd_error=1", status_code=303)

    hashed = hash_password(new_pwd)
    async with get_db_ctx() as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            ("admin_password_hash", hashed),
        )
        await db.commit()

    return RedirectResponse(url="/admin/?pwd_saved=1", status_code=303)


@router.get("/musicbot", response_class=HTMLResponse)
async def musicbot_settings(request: Request, user: dict = Depends(get_current_user)):
    async with get_db_ctx() as db:
        rows = await db.execute("SELECT key, value FROM bot_settings")
        settings_rows = await rows.fetchall()
        bot_settings = {row["key"]: row["value"] for row in settings_rows}
    return templates.TemplateResponse(
        "admin/musicbot.html",
        {"request": request, "user": user, "bot_settings": bot_settings},
    )


@router.post("/musicbot")
async def save_musicbot_settings(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    async with get_db_ctx() as db:
        for key in [
            "telegram_bot_token",
            "vk_token",
            "spotify_client_id",
            "spotify_client_secret",
            "acoustid_api_key",
            "default_quality",
            "results_per_page",
            "platform_priority",
        ]:
            value = form.get(key, "")
            await db.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()

    return RedirectResponse(url="/admin/musicbot?saved=1", status_code=303)


@router.get("/weatherbot", response_class=HTMLResponse)
async def weatherbot_settings(request: Request, user: dict = Depends(get_current_user)):
    async with get_db_ctx() as db:
        rows = await db.execute("SELECT key, value FROM bot_settings WHERE key = 'openweathermap_api_key'")
        settings_rows = await rows.fetchall()
        bot_settings = {row["key"]: row["value"] for row in settings_rows}
    return templates.TemplateResponse(
        "admin/weatherbot.html", 
        {"request": request, "user": user, "bot_settings": bot_settings}
    )

@router.post("/weatherbot")
async def save_weatherbot_settings(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    async with get_db_ctx() as db:
        key = "openweathermap_api_key"
        value = form.get(key, "")
        await db.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()

    return RedirectResponse(url="/admin/weatherbot?saved=1", status_code=303)


@router.post("/verify/telegram")
async def verify_telegram_token(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    token = form.get("telegram_bot_token")
    if not token:
        return {"ok": False, "error": "Токен не предоставлен"}
    
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get("ok"):
                    bot_username = data["result"].get("username", "")
                    return {"ok": True, "username": bot_username}
                else:
                    return {"ok": False, "error": data.get("description", "Unknown error")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/verify/weather")
async def verify_weather_api(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    key = form.get("openweathermap_api_key")
    if not key:
        return {"ok": False, "error": "Ключ не предоставлен"}
    
    url = f"https://api.openweathermap.org/data/2.5/weather?q=London&appid={key}"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if resp.status == 200:
                    return {"ok": True}
                else:
                    return {"ok": False, "error": data.get("message", "Unknown error")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
