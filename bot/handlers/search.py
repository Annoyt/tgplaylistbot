"""Text search handler: unified search across YouTube, VK, Spotify."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from difflib import SequenceMatcher

from aiogram import F, Router
from aiogram.types import Message

from app.db.models import TrackInfo
from bot.keyboards.inline import search_results_kb
from services import spotify as sp_svc
from services import vk_music as vk_svc
from services import youtube as yt_svc
from services.vk_captcha import captcha_manager

router = Router()
logger = logging.getLogger(__name__)

# In-memory cache for search results per user (user_id -> list[TrackInfo])
_MAX_CACHE = 500
_search_cache: OrderedDict[int, list[TrackInfo]] = OrderedDict()


def _format_results(tracks: list[TrackInfo], page: int, per_page: int, total: int) -> str:
    """Format search results as numbered list."""
    start = (page - 1) * per_page
    lines = [f"🔍 Результаты {start+1}-{min(start+per_page, total)} из {total}\n"]

    for i, t in enumerate(tracks[start:start+per_page], start=start+1):
        lines.append(
            f"{i}. {t.artist} – {t.title} "
            f"{t.duration_str} {t.source_icon} "
            f"{t.size_str} {t.bitrate_str}"
        )

    return "\n".join(lines)


@router.message(F.text & ~F.text.startswith("/") & ~F.text.startswith("http"))
async def text_search(message: Message) -> None:
    """Handle text search queries."""


    query = message.text.strip()
    if not query or len(query) < 2:
        return

    # Cross-Topic Routing: If requested in a specific topic, move it to General
    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    # Always send status to the General topic or DM
    status_msg = await message.bot.send_message(
        chat_id=message.chat.id,
        text=f"🔍 Ищу на всех платформах: <b>{query}</b>...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None),
        parse_mode="HTML"
    )

    try:
        # Parallel search
        c_handler = captcha_manager.get_captcha_handler(
            message.bot, 
            message.chat.id, 
            message.from_user.id
        )
        yt_task = yt_svc.search(query, count=30)
        vk_task = vk_svc.search(query, count=30, captcha_handler=c_handler)
        sp_task = sp_svc.search(query, count=30)

        results = await asyncio.gather(yt_task, vk_task, sp_task, return_exceptions=True)

        yt_res = results[0] if isinstance(results[0], list) else []
        vk_res = results[1] if isinstance(results[1], list) else []
        sp_res = results[2] if isinstance(results[2], list) else []

        platform_res = {
            "youtube": yt_res,
            "vk": vk_res,
            "spotify": sp_res
        }

        from app.db.database import get_db
        db = await get_db()
        try:
            row = await db.execute("SELECT platform_priority FROM user_settings WHERE user_id = ?", (message.from_user.id,))
            data = await row.fetchone()
            platforms = [p.strip() for p in data["platform_priority"].split(",")] if data else ["youtube", "vk", "spotify"]
        finally:
            await db.close()

        all_tracks: list[TrackInfo] = []
        for p in platforms:
            all_tracks.extend(platform_res.get(p, []))

        for r in results:
            if isinstance(r, Exception):
                logger.warning("Search error: %s", r)

        if not all_tracks:
            await status_msg.edit_text("😔 Ничего не найдено")
            return

        # Split "Artist - Title" so artist and title are matched separately;
        # otherwise treat the whole query as the title (word-order tolerant).
        q_artist, q_title = "", query
        for sep in (" — ", " – ", " - ", " -", "- "):
            if sep in query:
                left, _, right = query.partition(sep)
                if left.strip() and right.strip():
                    q_artist, q_title = left.strip(), right.strip()
                    break

        # Rank by closeness to what the user typed, then cache (evict oldest).
        all_tracks = rank_by_relevance(all_tracks, q_artist, q_title)
        user_id = message.from_user.id
        _search_cache[user_id] = all_tracks
        if len(_search_cache) > _MAX_CACHE:
            _search_cache.popitem(last=False)
        per_page = 10
        artist_name = all_tracks[0].artist if all_tracks else None
        text = _format_results(all_tracks, page=1, per_page=per_page, total=len(all_tracks))
        kb = search_results_kb(all_tracks, page=1, total=len(all_tracks), per_page=per_page, artist_name=artist_name)

        await status_msg.edit_text(text, reply_markup=kb)

    except Exception as e:
        logger.error("Search failed: %s", e)
        await status_msg.edit_text("❌ Произошла ошибка при поиске. Попробуйте позже.")


def get_cached_tracks(user_id: int) -> list[TrackInfo]:
    """Get cached search results for a user."""
    return _search_cache.get(user_id, [])


def sort_tracks(tracks: list[TrackInfo], sort_by: str) -> list[TrackInfo]:
    """Sort tracks by given criteria."""
    if sort_by == "bitrate_desc":
        return sorted(tracks, key=lambda t: t.bitrate, reverse=True)
    elif sort_by == "bitrate_asc":
        return sorted(tracks, key=lambda t: t.bitrate)
    elif sort_by in ("title", "asc"):
        return sorted(tracks, key=lambda t: t.title.lower())
    
    def default_sort_key(t: TrackInfo):
        # Primary: Duration (ascending)
        # Secondary: Estimated or real size (descending)
        # We calculate an effective size in MB for sorting
        if t.filesize > 0:
            eff_size = t.filesize
        else:
            # Estimate: YouTube tracks are usually around 128-160kbps
            # 160 kbps = 20 KB/s
            eff_size = t.duration * 20 * 1024
            
        return (t.duration, -eff_size)

    return sorted(tracks, key=default_sort_key)


def filter_lossless(tracks: list[TrackInfo]) -> list[TrackInfo]:
    """Filter to lossless tracks only."""
    return [t for t in tracks if t.is_lossless or t.bitrate >= 320]


# Markers of non-original edits. A track carrying one of these is penalised
# unless the query itself asks for it (so a search for "slowed" still works).
_EDIT_TAGS = (
    "slowed", "sped up", "spedup", "speed up", "nightcore", "reverb",
    "remix", "cover", "live", "karaoke", "instrumental", "8d audio", "8d",
    "bass boosted", "mashup", "tiktok", "ringtone", "acapella", "acoustic",
)

_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    """Lowercase, strip bracketed noise and punctuation for fuzzy comparison."""
    s = _NOISE_RE.sub(" ", s.lower())
    s = _NON_WORD_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


# Markers of podcasts / non-song long-form / compilation junk. A track carrying
# one of these (or running far longer than a song) is pushed down hard or dropped
# — unless the user's own query asks for that kind of content.
_JUNK_TAGS = (
    "podcast", "подкаст", "эпизод", "episode", "выпуск", "интервью",
    "interview", "стрим", "stream", "обзор", "review", "новости", "news",
    "лекция", "lecture", "разговор", "аудиокнига", "audiobook", "глава",
    "chapter", "проповедь", "сборник", "compilation", "megamix",
    "playlist", "плейлист", "лучшие песни", "greatest hits", "all songs",
    "full album", "целый альбом", "радио", "talk",
)

# Strongest markers — these alone justify dropping a result entirely (not just
# penalising), since they are virtually never a song the user searched for.
_HARD_JUNK_TAGS = (
    "podcast", "подкаст", "эпизод", "выпуск", "episode",
    "аудиокнига", "audiobook", "глава", "проповедь", "лекция",
    "интервью", "interview", "talk show",
)

# If the query itself contains one of these, the user WANTS long-form, so we
# skip the podcast/duration filtering entirely.
_LONGFORM_QUERY_TAGS = (
    "podcast", "подкаст", "mix", "микс", "сборник", "compilation",
    "album", "альбом", "playlist", "плейлист", "full", "целиком",
    "hour", "hours", "час", "часа", "часов", "lofi", "lo-fi",
    "live set", "концерт", "concert", "greatest hits", "audiobook",
    "аудиокнига", "лекция", "подряд",
)

_SONG_HARD_MAX_SEC = 1800   # 30 min — drop entirely (almost never a single song)
_SONG_LONG_SEC = 1200       # 20 min — heavy penalty
_SONG_SUSPECT_SEC = 600     # 10 min — light penalty


def _is_longform_query(q: str) -> bool:
    """True if the query explicitly asks for a mix/podcast/album/long content."""
    qn = _normalize(q)
    return any(tag in qn for tag in _LONGFORM_QUERY_TAGS)


def _token_set_ratio(a: str, b: str) -> float:
    """Order-independent word overlap (matches 'Believer Imagine Dragons')."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _junk_penalty(track: TrackInfo, q_norm: str, longform: bool) -> float:
    """Penalty for podcast/junk/over-long items.

    Nothing is dropped — the penalty is large enough to sink junk below every
    real song match (whose score is ~0..1.3), so it lands at the END of the list
    but stays available. Hard junk sinks further than soft junk.
    """
    if longform:
        return 0.0
    combined = _normalize(f"{track.artist} {track.title}")
    penalty = 0.0
    if any(tag in combined and tag not in q_norm for tag in _HARD_JUNK_TAGS):
        penalty += 2.0   # podcasts / audiobooks / episodes / interviews
    elif any(tag in combined and tag not in q_norm for tag in _JUNK_TAGS):
        penalty += 1.0   # compilations / greatest hits / megamix / playlists
    d = track.duration
    if d > _SONG_HARD_MAX_SEC:
        penalty += 1.5
    elif d > _SONG_LONG_SEC:
        penalty += 0.6
    elif d > _SONG_SUSPECT_SEC:
        penalty += 0.2
    return penalty


# Channels/markers that signal an official upload (highest-quality, canonical).
def _official_boost(track: TrackInfo) -> float:
    """Boost for official sources: VEVO, YouTube '… - Topic', 'official' tags."""
    artist = track.artist.lower()
    title = track.title.lower()
    boost = 0.0
    if "vevo" in artist or "- topic" in artist or artist.endswith("topic"):
        boost += 0.20
    if "official" in title:
        boost += 0.08
    return boost


def relevance_score(track: TrackInfo, query_artist: str, query_title: str) -> float:
    """Return 0..1 closeness of a track to the desired official artist/title."""
    q_title = _normalize(query_title)
    q_artist = _normalize(query_artist)
    t_title = _normalize(track.title)
    t_artist = _normalize(track.artist)
    combined_q = f"{q_artist} {q_title}".strip()
    combined_t = f"{t_artist} {t_title}".strip()

    # Best of title-only, full (artist+title), and order-independent word overlap,
    # so it works whether or not the platform splits artist/title like we do and
    # regardless of word order ("Believer Imagine Dragons" vs "Imagine Dragons …").
    score = max(
        SequenceMatcher(None, q_title, t_title).ratio(),
        SequenceMatcher(None, combined_q, combined_t).ratio(),
        _token_set_ratio(combined_q, combined_t),
    )

    # Reward exact substring containment (handles "feat." / extra words).
    if q_title and q_title in t_title:
        score += 0.12
    if q_artist and q_artist in combined_t:
        score += 0.12

    # Penalise remix/edit versions unless the user explicitly wanted them.
    for tag in _EDIT_TAGS:
        if tag in combined_t and tag not in combined_q:
            score -= 0.25
            break

    return max(0.0, min(score, 1.0))


def quality_score(track: TrackInfo) -> float:
    """0..1 estimate of audio quality — heavier / higher-bitrate ranks higher."""
    if track.is_lossless:
        return 1.0
    kbps = float(track.bitrate)
    if kbps <= 0 and track.filesize > 0 and track.duration > 0:
        # Derive bitrate from the real file weight (the "fattest" wins).
        kbps = track.filesize * 8 / track.duration / 1000
    if kbps <= 0:
        # No metadata yet (YouTube/Spotify before download); assume a mid value,
        # VK typically serves 320k.
        kbps = 320.0 if track.source == "vk" else 192.0
    return max(0.0, min(kbps / 320.0, 1.0))


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _duration_score(duration: int, ref: float) -> float:
    """1.0 near the consensus release length, decaying with distance from it.

    Penalises both over-long edits (extended/looped/slowed) and short snippets.
    """
    if ref <= 0 or duration <= 0:
        return 0.5  # neutral when we don't know
    return max(0.0, 1.0 - abs(duration - ref) / ref)


# Weights for the secondary criteria. Name relevance (0..1) still dominates, but
# quality is weighted enough to pull the higher-bitrate/lossless version of the
# *same* song to the top — "качественные вперёд" — without overriding a clearly
# better title match.
_W_QUALITY = 0.20
_W_DURATION = 0.10


def rank_by_relevance(
    tracks: list[TrackInfo],
    query_artist: str,
    query_title: str,
    min_score: float = 0.0,
) -> list[TrackInfo]:
    """Rank tracks for selection.

    Primary: closeness to the official artist/title. Secondary (tie-breakers):
    audio quality (heaviest/highest-bitrate) and duration closeness to the
    consensus release length. Podcasts / non-song long-form / compilation junk
    is dropped or penalised unless the query itself asks for it.
    `min_score` filters on the relevance component only.
    """
    raw_query = f"{query_artist} {query_title}".strip()
    longform = _is_longform_query(raw_query)
    q_norm = _normalize(raw_query)

    # Nothing is dropped: junk is just heavily penalised so it sinks to the end.
    kept = [
        (rel, t)
        for rel, t in ((relevance_score(t, query_artist, query_title), t) for t in tracks)
        if rel >= min_score
    ]
    if not kept:
        return []

    # Consensus ("official") duration ≈ median length across the real matches.
    ref = _median([float(t.duration) for _, t in kept if t.duration > 0])

    scored = [
        (
            rel
            + _W_QUALITY * quality_score(t)
            + _W_DURATION * _duration_score(t.duration, ref)
            + _official_boost(t)
            - _junk_penalty(t, q_norm, longform),
            t.filesize,  # tie-break: prefer the heaviest ("fattest") file
            -idx,        # then keep original (platform-priority) order
            t,
        )
        for idx, (rel, t) in enumerate(kept)
    ]
    scored.sort(key=lambda x: (-x[0], -x[1], -x[2]))
    return [t for *_, t in scored]




@router.message(F.text.regexp(r'https?://(?:m\.)?vk\.com/(?:music/playlist/|audio\?z=audio_playlist)(-?\d+)_(\d+)(?:(?:/|%2F|_)([a-zA-Z0-9]+))?'))
async def vk_playlist_url(message: Message) -> None:
    """Handle VK playlist URLs."""
    text = message.text.strip()

    # Extract owner_id, playlist_id, and access_key (if present)
    match = re.search(r'(?:music/playlist/|audio\?z=audio_playlist)(-?\d+)_(\d+)(?:(?:/|%2F|_)([a-zA-Z0-9]+))?', text)
    if not match:
        return

    owner_id, playlist_id, access_key = match.groups()
    access_key = access_key or ""

    if getattr(message.chat, 'is_forum', False) and getattr(message, 'message_thread_id', None) is not None:
        try:
            await message.delete()
        except Exception:
            pass

    status_msg = await message.bot.send_message(
        chat_id=message.chat.id,
        text="⏳ Получаю список треков из плейлиста ВК...",
        message_thread_id=None if message.chat.type != "private" else getattr(message, 'message_thread_id', None)
    )

    try:
        from services import vk_music as vk_svc
        tracks = await vk_svc.get_playlist_tracks(owner_id, playlist_id, access_key)

        if not tracks:
            await status_msg.edit_text("❌ Не удалось получить треки. Плейлист пуст, закрыт или недоступен.")
            return

        # Add tracks to playlist_queue
        import json
        from dataclasses import asdict

        from app.db.database import get_db

        db = await get_db()
        try:
            user_id = message.from_user.id
            orig_id = message.message_id

            # Save session for original_msg_id if needed, but we can just use message.message_id

            inserted = 0
            for t in tracks:
                await db.execute(
                    "INSERT INTO playlist_queue (chat_id, user_id, original_msg_id, track_json) VALUES (?, ?, ?, ?)",
                    (message.chat.id, user_id, orig_id, json.dumps(asdict(t)))
                )
                inserted += 1

            await db.commit()
            await status_msg.edit_text(f"✅ Найдено {inserted} треков в плейлисте. Добавлено в фоновую очередь загрузки.\nБот будет скачивать их постепенно (с паузами), чтобы избежать блокировки.")
        except Exception as e:
            logger.error(f"Failed to save playlist queue: {e}")
            await status_msg.edit_text("❌ Ошибка при добавлении в очередь.")
        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Playlist extraction failed: {e}")
        await status_msg.edit_text("❌ Произошла ошибка при получении плейлиста.")
