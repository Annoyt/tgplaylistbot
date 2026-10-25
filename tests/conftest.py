"""Shared test setup.

Settings are pinned through environment variables *before* anything imports
``config``: env vars beat the local ``.env``, so tests never read real secrets
or touch the real ``data/bot.db`` / download dir.
"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vkbot-tests-"))
os.environ.update({
    "SECRET_KEY": "test-secret-key-0123456789abcdef",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "test-pass",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_API_BASE_URL": "",
    "VK_TOKEN": "",
    "VK_LOGIN": "",
    "VK_PASSWORD": "",
    "SPOTIFY_CLIENT_ID": "",
    "SPOTIFY_CLIENT_SECRET": "",
    "ACOUSTID_API_KEY": "",
    "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP / 'bot.db'}",
    "DOWNLOAD_DIR": str(_TMP / "downloads"),
    "YTDLP_DIR": "",
})

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.db.models import TrackInfo  # noqa: E402
from config import settings  # noqa: E402


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Point the DB and download dir at a fresh per-test temp dir."""
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    monkeypatch.setattr(settings, "download_dir", str(tmp_path / "downloads"))
    return settings


@pytest.fixture
async def db(tmp_settings):
    """Initialised schema in a temp SQLite file."""
    from app.db.database import init_db
    await init_db()
    return tmp_settings.db_path


@pytest.fixture
async def client(db):
    """Async client for the FastAPI app, backed by the temp DB."""
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def make_track():
    """Factory for TrackInfo with sensible defaults."""
    def _make(title="Song", artist="Artist", duration=200, source="youtube",
              source_id="id1", **kw) -> TrackInfo:
        return TrackInfo(title=title, artist=artist, duration=duration,
                         source=source, source_id=source_id, **kw)
    return _make
