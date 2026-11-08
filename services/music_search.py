"""Search across sources in batches: the first ~5 pages now, more on request.

Every batch asks each source for its cheapest unit (YouTube serves ~20 results
per request, VK and Spotify page by offset), so a search costs one request per
source and "Ещё" (more) pulls the next batch only when the user wants it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.db.models import TrackInfo
from services import spotify as sp_svc
from services import vk_music as vk_svc
from services import youtube as yt_svc
from services.ranking import Query, rank

logger = logging.getLogger(__name__)

SOURCES = ("youtube", "vk", "spotify")
# Per-source batch: ~50-60 results = 5-6 pages of 10.
BATCH = {"youtube": 20, "vk": 30, "spotify": sp_svc.SEARCH_MAX_LIMIT}


@dataclass
class Cursor:
    """Where each source's next batch starts; sources that ran dry."""

    sources: tuple[str, ...] = SOURCES
    offsets: dict[str, int] = field(default_factory=dict)
    exhausted: set[str] = field(default_factory=set)

    @property
    def has_more(self) -> bool:
        return any(s not in self.exhausted for s in self.sources)


async def _search_source(source: str, query: str, offset: int, captcha_handler) -> list[TrackInfo]:
    count = BATCH[source]
    if source == "youtube":
        return await yt_svc.search(query, count=count, offset=offset)
    if source == "vk":
        return await vk_svc.search(query, count=count, captcha_handler=captcha_handler, offset=offset)
    return await sp_svc.search(query, count=count, offset=offset)


async def fetch_batch(query: str, cursor: Cursor, captcha_handler=None) -> list[TrackInfo]:
    """Next batch from every source that isn't exhausted, in ``cursor.sources`` order.

    A source is exhausted once it returns nothing (VK and Spotify return []
    when not configured). A failed source keeps its offset for the next try.
    """
    active = [s for s in cursor.sources if s in BATCH and s not in cursor.exhausted]
    results = await asyncio.gather(
        *(_search_source(s, query, cursor.offsets.get(s, 0), captcha_handler) for s in active),
        return_exceptions=True,
    )
    tracks: list[TrackInfo] = []
    for source, res in zip(active, results):
        if isinstance(res, BaseException):
            logger.warning("%s search failed: %s", source, res)
            continue
        cursor.offsets[source] = cursor.offsets.get(source, 0) + BATCH[source]
        if not res:
            cursor.exhausted.add(source)
        tracks.extend(res)
    return tracks


@dataclass
class SearchSession:
    """One user's results and how to fetch more of them."""

    query: str                       # text sent to the sources ("" = can't fetch more)
    rank_query: Query                # how batches are ranked
    tracks: list[TrackInfo]
    cursor: Cursor = field(default_factory=lambda: Cursor(exhausted=set(SOURCES)))
    min_score: float = 0.0
    loading: bool = False

    @property
    def has_more(self) -> bool:
        return bool(self.query) and self.cursor.has_more


def _key(t: TrackInfo) -> tuple[str, str]:
    return t.source, t.source_id


async def load_more(session: SearchSession, captcha_handler=None) -> int:
    """Append the next ranked batch; returns how many new tracks were added.

    The batch is ranked on its own and goes after what the user has already
    seen, so earlier pages don't reshuffle.
    """
    batch = await fetch_batch(session.query, session.cursor, captcha_handler)
    seen = {_key(t) for t in session.tracks}
    fresh: list[TrackInfo] = []
    for t in batch:
        if _key(t) not in seen:
            seen.add(_key(t))
            fresh.append(t)
    ranked = rank(fresh, session.rank_query, session.min_score)
    session.tracks.extend(ranked)
    return len(ranked)
