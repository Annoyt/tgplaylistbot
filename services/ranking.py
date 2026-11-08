"""Search result ranking: closest to the query first, the best copy first.

1. The query is analysed: "Artist Title" is split (explicitly by " - ", or by
   which words the results carry as the artist), an artist-only query
   ("Сломанный воздух" — most results are *by* it) is detected, and so are
   versions the user asked for ("believer live").
2. Each track gets a text relevance, a penalty for a version the user didn't
   ask for (live, cover, karaoke, remix, sped-up, acoustic, alternative —
   English and Russian markers) and a penalty for podcast/compilation junk.
3. The same song from different uploads and sources forms a group (same
   words, same version, length within 30%).
4. Groups are ordered by relevance, then popularity (copies found, YouTube
   views, the source's own rank) and whether an official upload is among them.
5. Inside a song, copies are split into recordings by length (within 8%).
   The popular, studio-length recording leads — a concert or a music-video
   cut runs longer than the track. Inside a recording the best copy leads:
   real bitrate first, then official uploads; lyric-video re-uploads last.

Nothing is dropped unless ``min_score`` asks for it.
"""

from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.db.models import TrackInfo

# ── text normalisation ─────────────────────────────────────────────────────

_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_CYR_RE = re.compile(r"[а-я]")
_LAT_RE = re.compile(r"[a-z]")
# Latin letters that look like Cyrillic ones ("Kино" with a Latin K is "Кино").
_LAT2CYR = str.maketrans("aceopxykmtbh", "асеорхукмтвн")


def _fix_homoglyphs(word: str) -> str:
    if _CYR_RE.search(word) and _LAT_RE.search(word):
        return word.translate(_LAT2CYR)
    return word


def normalize(s: str) -> str:
    """Lowercase, strip bracketed extras and punctuation, unify ё/е and homoglyphs."""
    s = _NOISE_RE.sub(" ", s.lower().replace("ё", "е"))
    s = _NON_WORD_RE.sub(" ", s)
    return " ".join(_fix_homoglyphs(w) for w in s.split())


def _words(s: str) -> list[str]:
    return normalize(s).split()


def _contains_words(haystack: str, needle: str) -> bool:
    """Whole-word containment ("one" is not in "someone")."""
    return bool(needle) and f" {needle} " in f" {haystack} "


def _token_set_ratio(a: str, b: str) -> float:
    """Order-independent word overlap (matches 'Believer Imagine Dragons')."""
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def _tag_pattern(tags) -> re.Pattern:
    """Whole-word matcher for literal tags (longest first).

    Substring matching gave false hits: "news" in "Huey Lewis & The News",
    "выпуск" in "Выпускной", "час" in "Сейчас", "official" in "unofficial".
    """
    alts = "|".join(re.escape(t) for t in sorted(set(tags), key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def _regex_pattern(fragments) -> re.Pattern:
    """Whole-word matcher for regex fragments (Russian stems end in ``\\w*``)."""
    return re.compile(rf"(?<!\w)(?:{'|'.join(fragments)})(?!\w)")


def _tags_in(text: str, pattern: re.Pattern) -> set[str]:
    """Tags found in ``text``. Run on raw lowercased text, NOT normalize():
    versions are usually bracketed ("Believer (Slowed + Reverb)") and
    normalize() strips brackets."""
    return {m.group(0) for m in pattern.finditer(text.lower())}


# ── versions ───────────────────────────────────────────────────────────────

_VERSION_FRAGMENTS = {
    # Bare "live" is a common title word ("Live and Let Die"), so only its
    # version forms count: "(Live)", "[Live in Vegas]", "- Live", "live at …".
    "live": (
        r"[(\[][^)\]]*\blive\b(?!-)[^)\]]*[)\]]", r"-\s*live", r"cam\s*[)\]]",
        r"live (?:at|in|from|on|aus|version|session|performance|recording|edition|show|video|concert|stream|set)",
        r"hd live",
        r"лайв", r"концерт\w*", r"concert\w*", r"unplugged", r"tour", r"тур",
        r"фестивал\w*", r"festival", r"нашестви\w*", r"madison square garden", r"wembley",
        r"arena", r"арен[аеуы]", r"лужник\w*", r"олимпийск\w*", r"stadium", r"стадион\w*",
        r"выступлени\w*", r"on stage", r"ледов\w* двор\w*", r"новая волна", r"late show",
        r"tonight show", r"jimmy kimmel", r"fan ?cam", r"фанкам",
        r"\d{1,2}\.\d{1,2}\.\d{2,4}",  # a concert date: "(ЦСКА Арена 15.05.2021)"
    ),
    "cover": (
        r"cover\w*", r"кавер\w*", r"made popular by", r"tribute", r"in the style of",
        r"played by", r"originally performed by",
        # sung in another language / by a choir: "Believer(рус)", "Numb(на русском)"
        r"рус", r"на русском", r"russian version", r"in russian", r"русск\w* верси\w*",
        r"хор", r"choir",
    ),
    "karaoke": (
        r"karaoke", r"караоке", r"минус", r"минусовк\w*", r"instrumental\w*", r"инструментал\w*",
        r"backing track", r"без вокала", r"a ?cappella", r"acapella", r"акапелл\w*", r"vocals only",
    ),
    "remix": (
        r"remix\w*", r"ремикс\w*", r"rmx", r"mash ?up", r"мэшап", r"мешап", r"bootleg",
        r"rework", r"extended mix", r"club mix", r"vip mix", r"dub",
    ),
    "speed": (
        r"slowed", r"reverb", r"sped up", r"spedup", r"speed up", r"nightcore", r"daycore",
        r"замедл\w*", r"ускор\w*", r"8d", r"bass ?boost\w*", r"tik ?tok", r"тикток\w*",
    ),
    "acoustic": (
        r"acoustic\w*", r"акустик\w*", r"акустич\w*", r"symphon\w*", r"симфони\w*",
        r"orchestra\w*", r"оркестр\w*", r"piano version",
    ),
}
_VERSION_RES = {name: _regex_pattern(frags) for name, frags in _VERSION_FRAGMENTS.items()}

# "(Minimal Rock Version)" is a different take; "(Album Version)" is the track.
_ALT_RE = re.compile(r"(?<!\w)(\w+)\s+(?:version|versión|версия)(?!\w)")
_PLAIN_VERSIONS = {
    "album", "original", "radio", "single", "remastered", "remaster", "explicit", "clean",
    "full", "studio", "official", "оригинальная", "альбомная", "полная", "студийная",
}

# How far a version the user didn't ask for sinks (relevance is 0..1).
VERSION_PENALTY = {
    "karaoke": 0.5, "speed": 0.45, "cover": 0.4, "remix": 0.35,
    "live": 0.35, "acoustic": 0.3, "alt": 0.2,
}
MISSING_VERSION_PENALTY = 0.2  # asked for "live", this copy isn't
# The query names an artist and this copy is by someone else (a cover, or a
# mislabelled upload): the title alone used to score it as a full match.
ARTIST_MISMATCH_PENALTY = 0.2


def versions_of(text: str) -> frozenset[str]:
    """Version kinds marked in ``text`` (title, artist, channel)."""
    low = text.lower()
    found = {name for name, rx in _VERSION_RES.items() if rx.search(low)}
    if any(m.group(1) not in _PLAIN_VERSIONS for m in _ALT_RE.finditer(low)):
        found.add("alt")
    return frozenset(found)


def track_versions(t: TrackInfo) -> frozenset[str]:
    return versions_of(f"{t.artist} {t.title} {t.uploader}")


def version_penalty(track_vers: frozenset[str], query_vers: frozenset[str]) -> float:
    unasked = track_vers - query_vers
    penalty = max((VERSION_PENALTY[v] for v in unasked), default=0.0)
    if query_vers - track_vers:
        penalty += MISSING_VERSION_PENALTY
    return penalty


# Lyric/translation re-uploads: the same recording, but a third-party copy.
_REUPLOAD_RE = _regex_pattern((
    r"lyrics?", r"lyric video", r"текст\w*", r"перевод\w*", r"subtitles?", r"legendado",
    r"tradução", r"traducida", r"letra", r"с субтитрами", r"sub\.? español",
))


def is_reupload(t: TrackInfo) -> bool:
    return bool(_REUPLOAD_RE.search(f"{t.title} {t.uploader}".lower()))


# ── junk: podcasts, compilations, over-long items ──────────────────────────

_JUNK_TAGS = (
    "podcast", "подкаст", "эпизод", "episode", "выпуск", "интервью",
    "interview", "стрим", "stream", "обзор", "review", "новости", "news",
    "лекция", "lecture", "разговор", "аудиокнига", "audiobook", "глава",
    "chapter", "проповедь", "сборник", "compilation", "megamix",
    "playlist", "плейлист", "лучшие песни", "greatest hits", "all songs",
    "full album", "целый альбом", "радио", "talk",
)
# These alone mean "not a song": podcasts, audiobooks, interviews.
_HARD_JUNK_TAGS = (
    "podcast", "подкаст", "эпизод", "выпуск", "episode",
    "аудиокнига", "audiobook", "глава", "проповедь", "лекция",
    "интервью", "interview", "talk show",
)
# A query containing one of these wants long-form content: no junk filtering.
_LONGFORM_QUERY_TAGS = (
    "podcast", "подкаст", "mix", "микс", "сборник", "compilation",
    "album", "альбом", "playlist", "плейлист", "full", "целиком",
    "hour", "hours", "час", "часа", "часов", "lofi", "lo-fi",
    "live set", "концерт", "concert", "greatest hits", "audiobook",
    "аудиокнига", "лекция", "подряд",
)
_JUNK_RE = _tag_pattern(_JUNK_TAGS)
_HARD_JUNK_RE = _tag_pattern(_HARD_JUNK_TAGS)
_LONGFORM_RE = _tag_pattern(_LONGFORM_QUERY_TAGS)

_SONG_HARD_MAX_SEC = 1800   # 30 min — almost never a single song
_SONG_LONG_SEC = 1200       # 20 min
_SONG_SUSPECT_SEC = 600     # 10 min


def is_longform_query(q: str) -> bool:
    """True if the query explicitly asks for a mix/podcast/album/long content."""
    return bool(_tags_in(q, _LONGFORM_RE))


def junk_penalty(track: TrackInfo, raw_query: str, longform: bool) -> float:
    """Penalty for podcast/junk/over-long items — large enough to sink them
    below every real match, without removing them."""
    if longform:
        return 0.0
    combined = f"{track.artist} {track.title} {track.uploader}"
    if _tags_in(combined, _HARD_JUNK_RE) - _tags_in(raw_query, _HARD_JUNK_RE):
        penalty = 2.0
    # Soft tags only in the title: in a name they are often a band
    # ("Huey Lewis & The News"); a podcast channel trips the hard tags.
    elif _tags_in(track.title, _JUNK_RE) - _tags_in(raw_query, _JUNK_RE):
        penalty = 1.0
    else:
        penalty = 0.0
    d = track.duration
    if d > _SONG_HARD_MAX_SEC:
        penalty += 1.5
    elif d > _SONG_LONG_SEC:
        penalty += 0.6
    elif d > _SONG_SUSPECT_SEC:
        penalty += 0.2
    return penalty


# ── officialness ───────────────────────────────────────────────────────────

_OFFICIAL_LABELS = (
    "vevo", "- topic", "sony music", "warner music", "universal music",
    "believe music", "atlantic records", "columbia records", "def jam",
    "capitol records", "republic records", "rca records", "interscope",
    "ego music", "napalm records", "roadrunner records", "official",
)
_OFFICIAL_RE = _tag_pattern(_OFFICIAL_LABELS)


def is_official(t: TrackInfo) -> bool:
    """Official upload: flagged by the source, or a label/VEVO/Topic channel."""
    if t.official:
        return True
    name = (t.uploader or t.artist).lower().strip()
    return name.endswith("topic") or name.endswith("vevo") or bool(_tags_in(name, _OFFICIAL_RE))


# ── query analysis ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Query:
    artist: str = ""
    title: str = ""
    artist_mode: bool = False          # the query names an artist, not a song
    versions: frozenset[str] = field(default_factory=frozenset)
    longform: bool = False

    @property
    def raw(self) -> str:
        return f"{self.artist} {self.title}".strip()


def artist_matches(t: TrackInfo, artist: str) -> bool:
    """All words of ``artist`` are in the track's artist ("Кино" ⊂ "Кино, В. Цой")."""
    want = set(_words(artist))
    if not want:
        return False
    have = set(_words(t.artist))
    if want <= have:
        return True
    compact = "".join(_words(artist))
    return len(compact) >= 3 and compact == "".join(_words(t.artist))  # "ImagineDragons"


def _infer_split(query: str, tracks: list[TrackInfo]) -> tuple[str, str]:
    """Split "Кино Группа крови" into artist/title by what the results say."""
    words = query.split()
    best, best_support = ("", query), 1  # need at least 2 supporting results
    for k in range(1, len(words)):
        for a_words, t_words in ((words[:k], words[k:]), (words[k:], words[:k])):
            artist, title = " ".join(a_words), " ".join(t_words)
            t_norm = normalize(title)
            support = sum(
                1 for t in tracks
                if artist_matches(t, artist) and _contains_words(normalize(t.title), t_norm)
            )
            if support > best_support:
                best, best_support = (artist, title), support
    return best


def analyze_query(artist: str, title: str, tracks: list[TrackInfo], *, artist_mode: bool = False) -> Query:
    """Build the ranking query from what the user typed and what was found."""
    raw = f"{artist} {title}".strip()
    if not artist and title and not artist_mode:
        by_it = sum(1 for t in tracks if artist_matches(t, title))
        if by_it >= 3 and by_it >= 0.25 * len(tracks):
            artist, title, artist_mode = title, "", True
        else:
            artist, title = _infer_split(title, tracks)
    elif artist and not title:
        artist_mode = True
    return Query(
        artist=artist, title=title, artist_mode=artist_mode,
        versions=versions_of(raw), longform=is_longform_query(raw),
    )


# ── scoring ────────────────────────────────────────────────────────────────

def text_relevance(track: TrackInfo, query_artist: str, query_title: str) -> float:
    """0..1 closeness of the track's artist/title to the query's."""
    q_title, q_artist = normalize(query_title), normalize(query_artist)
    t_title, t_artist = normalize(track.title), normalize(track.artist)
    combined_q = f"{q_artist} {q_title}".strip()
    combined_t = f"{t_artist} {t_title}".strip()
    if q_artist and q_title:
        # Both known: the title decides, the artist scales it. Comparing the
        # "artist title" strings made another song of the same artist a near
        # match ("Imagine Dragons Thunder" ~ "Imagine Dragons Believer": 0.93).
        title_sim = max(SequenceMatcher(None, q_title, t_title).ratio(), _token_set_ratio(q_title, t_title))
        if _contains_words(t_artist, q_artist):
            artist_sim = 1.0
        else:
            artist_sim = max(SequenceMatcher(None, q_artist, t_artist).ratio(), _token_set_ratio(q_artist, t_artist))
        score = title_sim * (0.7 + 0.3 * artist_sim)
    else:
        # Only a title (maybe with the artist's words in it, in any order):
        # best of title-only, full, and order-independent word overlap.
        score = max(
            SequenceMatcher(None, q_title, t_title).ratio() if q_title else 0.0,
            SequenceMatcher(None, combined_q, combined_t).ratio(),
            _token_set_ratio(combined_q, combined_t),
        )
    # Reward whole-word containment (handles "feat." / extra words).
    if _contains_words(t_title, q_title):
        score += 0.12
    if _contains_words(combined_t, q_artist):
        score += 0.12
    return min(score, 1.0)


def relevance(track: TrackInfo, q: Query) -> float:
    """Text relevance minus the unasked-version penalty (can go below 0)."""
    if q.artist_mode:
        if artist_matches(track, q.artist):
            base = 1.0
        else:
            # A song *named* like the artist, by someone else: below the artist.
            base = 0.85 * text_relevance(track, "", q.artist)
    else:
        base = text_relevance(track, q.artist, q.title)
        if q.artist and not artist_matches(track, q.artist):
            base -= ARTIST_MISMATCH_PENALTY
    return base - version_penalty(track_versions(track), q.versions)


def quality_score(t: TrackInfo) -> float:
    """0..1 audio quality of the file the user would get."""
    if t.is_lossless:
        return 1.0
    if t.source == "youtube":
        # Opus ~130-160k, converted on download: about a 224-256k MP3.
        return 0.75
    kbps = float(t.bitrate)
    if kbps <= 0 and t.filesize > 0 and t.duration > 0:
        kbps = t.filesize * 8 / t.duration / 1000
    if kbps <= 0:
        # Unmeasured: most VK copies are 320k; Spotify downloads via YouTube.
        return 0.8 if t.source == "vk" else 0.7
    return min(kbps / 320.0, 1.0)


# Copies of one recording differ by a few seconds (silence, fades); past
# this the length says "another edit", not "another upload".
LENGTH_TOLERANCE = 0.03


def duration_fit(duration: int, ref: float) -> float:
    """1.0 near the reference (studio) length, lower further away.

    Longer copies drop faster than shorter ones: a concert recording or a
    music-video cut with an intro runs longer than the track; a shorter one
    is more often a radio edit.
    """
    if ref <= 0 or duration <= 0:
        return 0.5
    d = (duration - ref) / ref
    excess = abs(d) - LENGTH_TOLERANCE
    if excess <= 0:
        return 1.0
    return max(0.0, 1.0 - (4.0 if d > 0 else 3.0) * excess)


def _rank_signal(t: TrackInfo) -> float:
    """1.0 for a source's top result, 0.5 at its 10th."""
    return 1.0 / (1.0 + t.source_rank / 10.0) if t.source_rank >= 0 else 0.0


# ── grouping ───────────────────────────────────────────────────────────────

_GROUP_NOISE = {
    "official", "video", "music", "audio", "lyrics", "lyric", "hd", "hq", "4k",
    "vevo", "topic", "feat", "ft", "prod", "remastered", "explicit", "the",
    "mv", "clip", "visualizer", "клип", "текст",
}


def track_tokens(t: TrackInfo) -> set[str]:
    """Significant words of a track (artist+title) for duplicate detection.

    The artist's words are also added glued together, so the channel-style
    "ImagineDragons" matches "Imagine Dragons".
    """
    artist = _words(t.artist)
    words = artist + _words(t.title)
    tokens = {w for w in words if w not in _GROUP_NOISE and len(w) > 1}
    if len(artist) > 1:
        tokens.add("".join(artist))
    return tokens


def artist_words(t: TrackInfo) -> set[str]:
    return {w for w in _words(t.artist) if len(w) > 1}


def _same_artist(a: TrackInfo, a_words: set[str], b: TrackInfo, b_words: set[str]) -> bool:
    """Shares at least half the words of the shorter name ("Кино, В. Цой" ~
    "Группа КИНО"), or the same name written together ("ImagineDragons");
    an unknown artist is compatible with anything."""
    if not a_words or not b_words:
        return True
    if _overlap(a_words, b_words, by_larger=False) >= 0.5:
        return True
    glued_a, glued_b = "".join(_words(a.artist)), "".join(_words(b.artist))
    return len(glued_a) >= 3 and glued_a == glued_b


def title_tokens(t: TrackInfo) -> set[str]:
    return {w for w in _words(t.title) if w not in _GROUP_NOISE and len(w) > 1}


def _overlap(a: set[str], b: set[str], *, by_larger: bool) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / (max if by_larger else min)(len(a), len(b))


@dataclass
class _Group:
    tokens: set[str]
    title: set[str]
    artist: set[str]
    versions: frozenset[str]
    anchor: TrackInfo
    duration: int
    members: list = field(default_factory=list)  # (track, rel, junk, index)


def _group(entries) -> list[_Group]:
    """Group same-song copies. ``entries`` come most relevant first, so each
    group is anchored at its most relevant copy."""
    groups: list[_Group] = []
    for entry in entries:
        t = entry[0]
        toks, title, vers = track_tokens(t), title_tokens(t), track_versions(t)
        artist = artist_words(t)
        for g in groups:
            if not toks or not g.tokens or vers != g.versions:
                continue  # e.g. "(Slowed)" vs the original
            # Artist+title words: containment (overlap / smaller set) tolerates
            # extra words of one upload. Title words: at least half of the
            # longer title, so "Хочешь" ≠ "Хочешь сладких апельсинов". Artists
            # must share a word, so a cover ("Radio Tapok") is its own song.
            # The length gate keeps same-artist songs apart.
            same_length = g.duration == 0 or t.duration == 0 or abs(t.duration - g.duration) <= 0.30 * g.duration
            if (
                same_length
                and _overlap(toks, g.tokens, by_larger=False) >= 0.70
                # without an artist, the artist's words may sit in the title
                and (not artist or not g.artist or _overlap(title, g.title, by_larger=True) >= 0.5)
                and _same_artist(t, artist, g.anchor, g.artist)
            ):
                g.members.append(entry)
                break
        else:
            groups.append(_Group(toks, title, artist, vers, t, t.duration, [entry]))
    return groups


def _is_studio_copy(t: TrackInfo) -> bool:
    """A copy whose length is the release's: label uploads, Topic/"Audio"
    uploads of the artist. Music videos often add an intro, lyric videos trim."""
    if is_reupload(t):
        return False
    if t.source in ("vk", "spotify"):
        return t.official
    low = t.title.lower()
    return t.uploader.lower().strip().endswith("topic") or (is_official(t) and "audio" in low and "video" not in low)


def _median_length(entries) -> float:
    known = [e[0].duration for e in entries if e[0].duration > 0]
    return float(statistics.median(known)) if known else 0.0


def _reference_length(members) -> float:
    """Studio length of a song: its studio copies, else official ones, else all."""
    for pick in (_is_studio_copy, is_official, lambda t: True):
        chosen = [e for e in members if pick(e[0]) and e[0].duration > 0]
        if chosen:
            return _median_length(chosen)
    return 0.0


# One recording = copies within this of each other's length (an album take
# and its uploads); a longer gap is another recording of the song.
RECORDING_TOLERANCE = 0.08


def _split_recordings(members) -> list[list]:
    """Cluster a song's copies by length into recordings."""
    known = sorted((e for e in members if e[0].duration > 0), key=lambda e: e[0].duration)
    recordings: list[list] = []
    for e in known:
        if recordings and e[0].duration <= recordings[-1][0][0].duration * (1 + RECORDING_TOLERANCE):
            recordings[-1].append(e)
        else:
            recordings.append([e])
    unknown = [e for e in members if e[0].duration <= 0]
    if unknown:
        if recordings:
            max(recordings, key=len).extend(unknown)
        else:
            recordings.append(unknown)
    return recordings


def _copy_score(t: TrackInfo, ref: float) -> float:
    """Order inside a recording: quality first, then officialness, length."""
    return (
        quality_score(t)
        + 0.15 * is_official(t)
        - 0.15 * is_reupload(t)
        + 0.3 * duration_fit(t.duration, ref)
        + 0.05 * _rank_signal(t)
    )


# Weights of the song order; relevance (~0..1) dominates.
W_POPULARITY = 0.30
W_OFFICIAL = 0.12
W_QUALITY = 0.08
# Weight of the closeness to the studio length when ordering recordings.
W_LENGTH = 0.30


def _popularity(members, max_views: int) -> float:
    """0..1 from copies found, YouTube views (relative to the batch) and the
    sources' own order."""
    size = min(1.0, math.log1p(len(members)) / math.log1p(12))
    views = max(t.views for t, *_ in members)
    view_s = math.log1p(views) / math.log1p(max_views) if max_views > 0 else 0.0
    rank_s = max(_rank_signal(t) for t, *_ in members)
    return 0.4 * size + 0.4 * view_s + 0.2 * rank_s


def _order_song(members, max_views: int) -> list[TrackInfo]:
    """A song's copies: the most popular, studio-length recording first; inside
    a recording the best-quality copy first."""
    song_ref = _reference_length(members)

    def recording_score(rec) -> float:
        return (
            _popularity(rec, max_views)
            + W_OFFICIAL * any(is_official(t) for t, *_ in rec)
            + W_LENGTH * duration_fit(int(_median_length(rec)), song_ref)
        )

    recordings = sorted(_split_recordings(members), key=lambda r: (-recording_score(r), min(e[3] for e in r)))
    out: list[TrackInfo] = []
    for rec in recordings:
        ref = _median_length(rec)
        out.extend(e[0] for e in sorted(rec, key=lambda e: (-_copy_score(e[0], ref), -e[0].filesize, e[3])))
    return out


def rank(tracks: list[TrackInfo], q: Query, min_score: float = 0.0) -> list[TrackInfo]:
    """Rank ``tracks`` for ``q`` (see the module docstring)."""
    raw = q.raw
    scored = []
    for i, t in enumerate(tracks):
        rel = relevance(t, q)
        # Penalties can take relevance below 0; only a real threshold drops.
        if min_score <= 0 or rel >= min_score:
            scored.append((t, rel, junk_penalty(t, raw, q.longform), i))
    if not scored:
        return []
    scored.sort(key=lambda e: (-(e[1] - e[2]), e[3]))
    max_views = max(t.views for t, *_ in scored)

    songs = []
    for g in _group(scored):
        score = (
            max(rel - junk for _, rel, junk, _ in g.members)
            + W_POPULARITY * _popularity(g.members, max_views)
            + W_OFFICIAL * any(is_official(t) for t, *_ in g.members)
            + W_QUALITY * max(quality_score(t) for t, *_ in g.members)
        )
        songs.append((score, g.members[0][3], _order_song(g.members, max_views)))
    songs.sort(key=lambda s: (-s[0], s[1]))
    return [t for *_, copies in songs for t in copies]


def rank_by_relevance(
    tracks: list[TrackInfo], query_artist: str, query_title: str, min_score: float = 0.0,
) -> list[TrackInfo]:
    """Rank for what the user typed (artist may be empty)."""
    return rank(tracks, analyze_query(query_artist, query_title, tracks), min_score)


def best_match(reference: TrackInfo, candidates: list[TrackInfo]) -> TrackInfo | None:
    """The candidate closest to ``reference`` (a Spotify track to fetch from
    YouTube): same song and version, studio length, official upload."""
    if not candidates:
        return None
    q = Query(artist=reference.artist, title=reference.title, versions=track_versions(reference))

    def score(c: TrackInfo) -> float:
        return relevance(c, q) + 0.5 * duration_fit(c.duration, reference.duration) + 0.1 * is_official(c)

    return max(candidates, key=score)
