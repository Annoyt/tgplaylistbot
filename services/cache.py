import re
import logging
from app.db.database import get_db

logger = logging.getLogger(__name__)

def _normalize_name(name: str) -> str:
    """Normalize track/artist name for robust caching."""
    if not name:
        return ""
    # Lowercase, remove special characters, extra spaces, featured artists (ft., feat.)
    n = name.lower()
    n = re.sub(r'\(.*?\)|\[.*?\]', '', n) # remove brackets
    n = re.sub(r'\b(feat\.?|ft\.?)\s+.*', '', n) # remove featured
    n = re.sub(r'[^a-zа-я0-9\s]', '', n) # remove non-alphanumeric except spaces
    n = re.sub(r'\s+', ' ', n).strip() # normalize spaces
    return n

def generate_track_hash(artist: str, title: str) -> str:
    """Generate a normalized hash string for a track."""
    norm_artist = _normalize_name(artist)
    norm_title = _normalize_name(title)
    return f"{norm_artist}_{norm_title}"

async def get_cached_file_id(artist: str, title: str) -> str | None:
    """Retrieve Telegram file_id from cache if available."""
    track_hash = generate_track_hash(artist, title)
    if not track_hash or track_hash == "_":
        return None

    try:
        db = await get_db()
        row = await db.execute("SELECT file_id FROM cached_tracks WHERE artist_title_hash = ?", (track_hash,))
        res = await row.fetchone()
        await db.close()
        if res:
            logger.info(f"Cache hit for '{artist} - {title}': {res['file_id']}")
            return res["file_id"]
    except Exception as e:
        logger.error(f"Cache lookup failed for '{track_hash}': {e}")
    return None

async def save_cached_file_id(artist: str, title: str, file_id: str) -> bool:
    """Save Telegram file_id to cache."""
    track_hash = generate_track_hash(artist, title)
    if not track_hash or track_hash == "_":
        return False

    try:
        db = await get_db()
        await db.execute(
            "INSERT OR IGNORE INTO cached_tracks (artist_title_hash, file_id) VALUES (?, ?)",
            (track_hash, file_id)
        )
        await db.commit()
        await db.close()
        logger.info(f"Saved cache for '{artist} - {title}'")
        return True
    except Exception as e:
        logger.error(f"Cache save failed for '{track_hash}': {e}")
        return False
