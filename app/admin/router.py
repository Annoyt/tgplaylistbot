"""Admin panel router — protected by JWT auth."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user
from app.db.database import get_db_ctx

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request, user: dict = Depends(get_current_user)):
    return templates.TemplateResponse("admin/dashboard.html", {"request": request, "user": user})


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
    return templates.TemplateResponse("admin/weatherbot.html", {"request": request, "user": user})
