"""Spotify metadata search via spotipy. Download delegated to YouTube."""

from __future__ import annotations

import logging

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from app.db.models import TrackInfo
from config import settings

logger = logging.getLogger(__name__)


def _get_sp() -> spotipy.Spotify | None:
    if not settings.spotify_client_id or not settings.spotify_client_secret:
        return None
    try:
        auth = SpotifyClientCredentials(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
        )
        # No retries: spotipy retries 429s honouring Retry-After (up to hours),
        # which stalled every search/recognition waiting on Spotify.
        return spotipy.Spotify(auth_manager=auth, requests_timeout=5, retries=0, status_retries=0)
    except Exception as e:
        logger.error("Spotify init failed: %s", e)
        return None


# Since February 2026 GET /search caps ``limit`` at 10 (was 50) for apps in
# development mode; a larger value is rejected, so every search failed.
SEARCH_MAX_LIMIT = 10


async def search(query: str, count: int = SEARCH_MAX_LIMIT, offset: int = 0) -> list[TrackInfo]:
    """Search Spotify for track metadata; ``offset`` pages through the results."""
    sp = _get_sp()
    if sp is None:
        return []

    try:
        import asyncio

        results = await asyncio.wait_for(
            asyncio.to_thread(
                sp.search, q=query, limit=max(1, min(count, SEARCH_MAX_LIMIT)), offset=offset, type="track",
            ),
            timeout=15,
        )
        items = results.get("tracks", {}).get("items", [])

        tracks: list[TrackInfo] = []
        for i, item in enumerate(items):
            duration_ms = item.get("duration_ms", 0)
            artists = ", ".join(a["name"] for a in item.get("artists", []))
            album = item.get("album", {}).get("name", "")
            cover = ""
            images = item.get("album", {}).get("images", [])
            if images:
                cover = images[0].get("url", "")

            tracks.append(
                TrackInfo(
                    title=item.get("name", "Unknown"),
                    artist=artists,
                    duration=duration_ms // 1000,
                    source="spotify",
                    source_id=item.get("id", ""),
                    bitrate=0,
                    filesize=0,
                    thumbnail_url=cover,
                    album=album,
                    # Label releases: their length is the studio reference.
                    official=True,
                    source_rank=offset + i,
                )
            )
        return tracks
    except Exception as e:
        logger.error("Spotify search failed: %s", e)
        return []
