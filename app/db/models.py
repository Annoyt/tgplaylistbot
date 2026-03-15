"""Data models / helpers for database operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UserSettings:
    user_id: int
    default_quality: str = "mp3_320"
    results_per_page: int = 10
    platform_priority: str = "youtube,vk,spotify"

    @property
    def platforms(self) -> list[str]:
        return [p.strip() for p in self.platform_priority.split(",")]


@dataclass
class ForumTopic:
    chat_id: int
    topic_id: int
    emoji: str
    name: str
    is_search: bool = False


@dataclass
class TrackInfo:
    """Unified track info across all platforms. Not stored on server."""

    title: str
    artist: str
    duration: int  # seconds
    source: str  # 'youtube' | 'vk' | 'spotify'
    source_id: str  # platform-specific ID / URL
    bitrate: int = 0  # kbps, 0 = unknown
    filesize: int = 0  # bytes, 0 = unknown
    is_lossless: bool = False
    thumbnail_url: str = ""
    album: str = ""

    @property
    def duration_str(self) -> str:
        m, s = divmod(self.duration, 60)
        return f"{m}:{s:02d}"

    @property
    def size_str(self) -> str:
        if self.filesize <= 0:
            return "—"
        mb = self.filesize / (1024 * 1024)
        return f"{mb:.1f}M"

    @property
    def bitrate_str(self) -> str:
        if self.is_lossless:
            return "FLAC"
        if self.bitrate <= 0:
            return "—"
        return f"{self.bitrate}k"

    @property
    def source_icon(self) -> str:
        icons = {"youtube": "▶️YT", "vk": "🎵VK", "spotify": "🟢SP"}
        return icons.get(self.source, self.source)


@dataclass
class RecognizedTrack:
    """Result from Shazam / AcoustID recognition."""

    title: str
    artist: str
    album: str = ""
    duration: int = 0
    cover_url: str = ""

@dataclass
class User:
    id: int
    username: str
    nickname: Optional[str] = None
    is_admin: int = 0
