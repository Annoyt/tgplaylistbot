"""Typed application settings loaded from .env file."""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Web Platform ──────────────────────────────────
    secret_key: str = "change-me-to-a-random-string"
    admin_username: str = "admin"
    admin_password: str = "change-me"

    # ── OpenWeatherMap ────────────────────────────────
    openweathermap_api_key: str = ""

    # ── Telegram MusicBot ─────────────────────────────
    telegram_bot_token: str = ""

    # ── VK Music ──────────────────────────────────────
    vk_token: str = ""
    vk_login: str = ""
    vk_password: str = ""

    # ── Spotify ───────────────────────────────────────
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    # ── AcoustID ──────────────────────────────────────
    acoustid_api_key: str = ""

    # ── Limits ────────────────────────────────────────
    max_file_size_mb: int = 49
    download_dir: str = "/tmp/musicbot"

    # ── Database ──────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:////app/data/bot.db"

    @property
    def db_path(self) -> Path:
        """Return resolved database file path."""
        raw = self.database_url.replace("sqlite+aiosqlite:///", "")
        return Path(raw)

    @property
    def download_path(self) -> Path:
        p = Path(self.download_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
