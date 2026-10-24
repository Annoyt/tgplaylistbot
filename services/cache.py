import re
import logging
from app.db.database import get_db_ctx

logger = logging.getLogger(__name__)

def _normalize_name(name: str) -> str:
    """Normalize track/artist name for robust caching."""
    if not name:
        return ""
    # Lowercase, remove special characters, extra spaces, featured artists (ft., feat.)
    n = name.lower()
    n = re.sub(r'\(.*?\)|\[.*?\]', '', n) # remove brackets
    n = re.sub(r'\b(feat\.?|ft\.?)\s+.*', '', n) # remove featured
    # \w, not [a-zа-я0-9]: that wiped CJK/Hangul/Arabic/accented letters and
    # "ё", so different songs collided on keys like "__253".
    n = re.sub(r'[^\w\s]', '', n)
    n = re.sub(r'\s+', ' ', n).strip() # normalize spaces
    return n

def generate_track_hash(artist: str, title: str, duration: int = 0) -> str:
    """Normalized cache key for a track (with its duration); "" = don't cache.

    A name that normalizes to nothing would make the key ambiguous, so such
    tracks are not cached at all.
    """
    norm_artist = _normalize_name(artist)
    norm_title = _normalize_name(title)
    if not norm_artist or not norm_title:
        return ""
    # Include duration to distinguish between versions (Live, Radio Edit, etc.)
    return f"{norm_artist}_{norm_title}_{duration}"

async def get_cached_file_id(artist: str, title: str, duration: int = 0) -> str | None:
    """Retrieve Telegram file_id from cache if available."""
    track_hash = generate_track_hash(artist, title, duration)
    if not track_hash:
        return None

    try:
        async with get_db_ctx() as db:   # closes the connection on errors too
            row = await db.execute("SELECT file_id FROM cached_tracks WHERE artist_title_hash = ?", (track_hash,))
            res = await row.fetchone()
        if res:
            logger.info(f"Cache hit for '{artist} - {title}': {res['file_id']}")
            return res["file_id"]
    except Exception as e:
        logger.error(f"Cache lookup failed for '{track_hash}': {e}")
    return None

async def save_cached_file_id(artist: str, title: str, duration: int, file_id: str) -> bool:
    """Save Telegram file_id to cache."""
    track_hash = generate_track_hash(artist, title, duration)
    if not track_hash:
        return False

    try:
        async with get_db_ctx() as db:
            # REPLACE, not IGNORE: a stale file_id that failed to send must be
            # overwritten by the fresh one, or every request re-downloads.
            await db.execute(
                "INSERT OR REPLACE INTO cached_tracks (artist_title_hash, file_id) VALUES (?, ?)",
                (track_hash, file_id)
            )
            await db.commit()
        logger.info(f"Saved cache for '{artist} - {title}'")
        return True
    except Exception as e:
        logger.error(f"Cache save failed for '{track_hash}': {e}")
        return False
