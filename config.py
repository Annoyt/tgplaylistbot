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

    # ── Local Bot API server (optional) ───────────────
    # Set to e.g. "http://telegram-bot-api:8081" to send files up to 2 GB.
    # Empty = use Telegram's cloud API (50 MB send / 20 MB download limit).
    telegram_api_base_url: str = ""

    # ── Audiobooks ────────────────────────────────────
    audiobook_bitrate_kbps: int = 64       # mono speech; 64k is plenty
    audiobook_part_size_mb: int = 45       # target size per part when splitting

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

    @property
    def cookies_path(self) -> Path:
        """Netscape-format cookies file for yt-dlp.

        Stored next to the database so it lives inside the persisted data
        volume (``/app/data`` in Docker) and survives container restarts.
        """
        return self.db_path.parent / "cookies.txt"

    def cookie_args(self) -> list[str]:
        """yt-dlp ``--cookies`` args if a cookies file is present, else empty."""
        p = self.cookies_path
        return ["--cookies", str(p)] if p.exists() else []

    @property
    def local_bot_api(self) -> bool:
        """True when a local Bot API server is configured (enables 2 GB sends)."""
        return bool(self.telegram_api_base_url.strip())

    @property
    def send_limit_bytes(self) -> int:
        """Max file size the bot can send: 2 GB via local server, else 50 MB."""
        return 2000 * 1024 * 1024 if self.local_bot_api else 50 * 1024 * 1024


settings = Settings()
