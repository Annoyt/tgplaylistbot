"""Auth router: login / logout endpoints."""

import asyncio
import time
from collections import deque

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import settings
from app.auth.service import (
    create_access_token,
    env_password_is_default,
    hash_password,
    secret_key_is_safe,
    verify_password,
)
from app.db.database import get_db_ctx

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")

# Pre-hash admin password at startup for comparison
_admin_hash = hash_password(settings.admin_password)

# Failed logins per client IP (the dashboard is public and gets brute-forced).
LOGIN_MAX_FAILURES = 10
LOGIN_WINDOW_SEC = 15 * 60
_failures: dict[str, deque] = {}


def _recent_failures(ip: str, now: float) -> deque:
    hits = _failures.setdefault(ip, deque())
    while hits and hits[0] <= now - LOGIN_WINDOW_SEC:
        hits.popleft()
    if len(_failures) > 10_000:  # bound memory under a distributed attack
        for k in [k for k, v in _failures.items() if not v]:
            del _failures[k]
    return hits


def _login_error(request: Request, error: str, status_code: int):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": error}, status_code=status_code
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(request: Request):
    if not secret_key_is_safe():
        return _login_error(request, "Вход отключён: задайте SECRET_KEY в .env (случайная строка от 32 символов).", 503)

    ip = request.client.host if request.client else "?"
    now = time.monotonic()
    failures = _recent_failures(ip, now)
    if len(failures) >= LOGIN_MAX_FAILURES:
        return _login_error(request, "Слишком много неудачных попыток. Попробуйте через 15 минут.", 429)

    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if not isinstance(username, str) or not isinstance(password, str):
        username, password = "", ""  # a file part instead of a text field

    # Check for custom password in DB, fallback to .env hash
    db_hash = None
    async with get_db_ctx() as db:
        rows = await db.execute("SELECT value FROM bot_settings WHERE key = 'admin_password_hash'")
        row = await rows.fetchone()
        if row and row["value"]:
            db_hash = row["value"]

    if not db_hash and env_password_is_default():
        return _login_error(request, "Вход отключён: задайте ADMIN_PASSWORD в .env.", 503)
    valid_hash = db_hash or _admin_hash

    # bcrypt takes ~0.25s: off the event loop so a brute-force can't stall the
    # bot, and always run so a wrong username isn't revealed by a faster reply.
    password_ok = await asyncio.to_thread(verify_password, password, valid_hash)
    if username == settings.admin_username and password_ok:
        failures.clear()
        token = create_access_token({"sub": username, "role": "admin"})
        response = RedirectResponse(url="/admin/", status_code=303)
        response.set_cookie(
            key="access_token",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=86400,
        )
        return response

    failures.append(now)
    return _login_error(request, "Неверный логин или пароль", 401)


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response
