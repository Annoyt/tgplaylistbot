"""FastAPI application: mounts all routers, serves static + templates."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth.router import router as auth_router
from app.weather.router import router as weather_router
from app.admin.router import router as admin_router
from app.db.database import init_db


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup / shutdown events."""
    await init_db()
    yield


app = FastAPI(title="VK Music Bot Platform", lifespan=lifespan)

# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Routers
app.include_router(auth_router)
app.include_router(weather_router)
app.include_router(admin_router)

templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})
