"""Auth router: login / logout endpoints."""

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import settings
from app.auth.service import create_access_token, verify_password, hash_password
from app.db.database import get_db_ctx

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")

# Pre-hash admin password at startup for comparison
_admin_hash = hash_password(settings.admin_password)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
async def login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    # Check for custom password in DB, fallback to .env hash
    db_hash = None
    async with get_db_ctx() as db:
        rows = await db.execute("SELECT value FROM bot_settings WHERE key = 'admin_password_hash'")
        row = await rows.fetchone()
        if row and row["value"]:
            db_hash = row["value"]
            
    valid_hash = db_hash if db_hash else _admin_hash

    if username == settings.admin_username and verify_password(password, valid_hash):
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

    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie("access_token")
    return response
