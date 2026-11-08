"""services/ranking.py: query analysis, versions, quality, grouping, order."""

import json
from pathlib import Path

import pytest

from app.db.models import TrackInfo
from services import ranking as R
from services.bitrate_probe import nominal_kbps
from services.youtube import is_official_upload, split_video_title


def T(title="Song", artist="Artist", duration=200, source="youtube", **kw) -> TrackInfo:
    kw.setdefault("source_id", f"{source}:{artist}:{title}:{duration}:{kw.get('bitrate', 0)}")
    return TrackInfo(title=title, artist=artist, duration=duration, source=source, **kw)


def Q(artist="", title="", **kw) -> R.Query:
    return R.Query(artist=artist, title=title, **kw)


# ── normalisation ──────────────────────────────────────────────────────────

class TestNormalize:
    def test_strips_brackets_punctuation_and_case(self):
        assert R.normalize("Believer (Official Video) [HD] - Imagine!") == "believer imagine"

    def test_collapses_whitespace(self):
        assert R.normalize("  a   b\tc ") == "a b c"

    def test_cyrillic_and_yo(self):
        assert R.normalize("Кино — Ёлка") == "кино елка"

    def test_latin_lookalikes_in_cyrillic_words(self):
        # "Kино" with a Latin K (and Latin "o") is the same band as "Кино"
        assert R.normalize("Kинo") == "кино"
        assert R.normalize("Cплин BЫХОДА") == "сплин выхода"

    def test_pure_latin_words_untouched(self):
        assert R.normalize("Kino Moscow") == "kino moscow"

    def test_contains_words_is_whole_word(self):
        assert R._contains_words("someone like you", "like you")
        assert not R._contains_words("someone", "one")
        assert not R._contains_words("abc", "")


class TestTokenSetRatio:
    def test_order_independent(self):
        assert R._token_set_ratio("believer imagine dragons", "imagine dragons believer") == 1.0

    def test_divides_by_larger_set(self):
        assert R._token_set_ratio("a b", "a b c d") == 0.5

    def test_empty_side_is_zero(self):
        assert R._token_set_ratio("", "a") == 0.0
        assert R._token_set_ratio("a", "") == 0.0


# ── versions ───────────────────────────────────────────────────────────────

class TestVersions:
    @pytest.mark.parametrize("text", [
        "Believer (Live)", "Believer [Live in Vegas]", "Numb - Live", "Live at Wembley",
        "Хочешь? (концерт «Маленький человек»)", "Группа крови. (ЦСКА Арена 15.05.2021)",
        "Кукла колдуна (Нашествие, 2011)", "Blinding Light (4K Live 2024)", "Believer - Pinkpop (HD Live Show)",
        "Группа Крови (rare) | (VIKTOR TSOI CAM)", "Sonne from madison square garden",
        "Bohemian Rhapsody 1981 Live Video", "Numb (2017 One More Light World Tour)",
    ])
    def test_live(self, text):
        assert "live" in R.versions_of(text)

    @pytest.mark.parametrize("text", ["Live and Let Die", "Live Forever", "Oliver Tree - Alive", "Delivery"])
    def test_live_as_a_title_word_is_not_a_version(self, text):
        assert "live" not in R.versions_of(text)

    @pytest.mark.parametrize("text,kind", [
        ("Believer (Imagine Dragons cover)", "cover"), ("Группа крови (кавер)", "cover"),
        ("Believer(рус)", "cover"), ("Numb(на русском)", "cover"), ("Imagine Dragons хор", "cover"),
        ("Believer (Made Popular By Imagine Dragons)", "cover"),
        ("Кукла Колдуна (Караоке)", "karaoke"), ("Хочешь (минус)", "karaoke"), ("Numb (Instrumental)", "karaoke"),
        ("Sonne (Remix)", "remix"), ("Группа крови (ремикс 2010)", "remix"), ("x (Club Mix)", "remix"),
        ("Believer (Slowed + Reverb)", "speed"), ("Хочешь (замедленная)", "speed"), ("x (sped up)", "speed"),
        ("Хочешь (Акустика)", "acoustic"), ("Sonne (Symphonic)", "acoustic"), ("Кино (оркестр)", "acoustic"),
        ("Нити (Minimal Rock Version)", "alt"), ("Хочешь (новая версия)", "alt"),
    ])
    def test_kinds(self, text, kind):
        assert kind in R.versions_of(text)

    @pytest.mark.parametrize("text", [
        "Believer (Album Version)", "Song (Radio Version)", "Song (Remastered Version)", "Хочешь (оригинальная версия)",
        "Believer (Official Audio)", "Discovery", "Group (Live-in nanny)",
    ])
    def test_plain_releases_have_no_version(self, text):
        assert R.versions_of(text) == frozenset()

    def test_track_versions_reads_artist_and_channel(self):
        assert "acoustic" in R.track_versions(T(artist="Король и Шут (Акустический альбом)", title="Кукла"))
        assert "karaoke" in R.track_versions(T(title="Кукла", uploader="Калинка Караоке"))

    def test_penalty_is_the_worst_unasked_version(self):
        assert R.version_penalty(frozenset({"live", "karaoke"}), frozenset()) == 0.5
        assert R.version_penalty(frozenset({"live"}), frozenset({"live"})) == 0.0
        assert R.version_penalty(frozenset(), frozenset()) == 0.0

    def test_missing_asked_version_is_penalised(self):
        assert R.version_penalty(frozenset(), frozenset({"live"})) == R.MISSING_VERSION_PENALTY
        assert R.version_penalty(frozenset({"remix"}), frozenset({"live"})) == pytest.approx(0.35 + 0.2)

    @pytest.mark.parametrize("title,uploader,expected", [
        ("Believer (Lyrics)", "", True), ("Хочешь | ТЕКСТ ПЕСНИ", "", True),
        ("Song (Sub. Español)", "", True), ("Song", "Richie Lyrics", True),
        ("Believer (Official Audio)", "", False),
    ])
    def test_reupload(self, title, uploader, expected):
        assert R.is_reupload(T(title=title, uploader=uploader)) is expected


# ── junk ───────────────────────────────────────────────────────────────────

class TestJunk:
    @pytest.mark.parametrize("q", ["lofi hip hop", "Подкаст про музыку", "full album", "2 часа музыки"])
    def test_longform_query(self, q):
        assert R.is_longform_query(q)

    @pytest.mark.parametrize("q", ["Imagine Dragons Believer", "Кино Группа крови", "Сейчас", "Imagine Dragons remix"])
    def test_song_query(self, q):
        assert not R.is_longform_query(q)

    def test_longform_disables_penalty(self):
        assert R.junk_penalty(T(title="Podcast episode 5", duration=4000), "podcast", longform=True) == 0.0

    def test_hard_and_soft_tags(self):
        assert R.junk_penalty(T(title="Interview with X"), "x", False) == 2.0
        assert R.junk_penalty(T(title="Greatest Hits"), "x", False) == 1.0

    def test_hard_tag_in_channel_counts(self):
        assert R.junk_penalty(T(title="Talk #1", uploader="Lex Fridman Podcast"), "x", False) == 2.0

    def test_soft_tag_in_name_ignored(self):
        assert R.junk_penalty(T(artist="Huey Lewis & The News", title="The Power of Love"), "x", False) == 0.0
        assert R.junk_penalty(T(artist="Баста", title="Выпускной"), "x", False) == 0.0

    def test_tag_in_query_not_penalised(self):
        assert R.junk_penalty(T(title="Greatest Hits"), "Queen greatest hits", False) == 0.0

    @pytest.mark.parametrize("dur,expected", [(600, 0.0), (601, 0.2), (1200, 0.2), (1201, 0.6), (1800, 0.6), (1801, 1.5)])
    def test_duration_thresholds(self, dur, expected):
        assert R.junk_penalty(T(title="Song", duration=dur), "song", False) == pytest.approx(expected)

    def test_hard_junk_and_overlong_stack(self):
        assert R.junk_penalty(T(title="Podcast", duration=4000), "x", False) == pytest.approx(3.5)


# ── officialness ───────────────────────────────────────────────────────────

class TestOfficial:
    def test_source_flag(self):
        assert R.is_official(T(official=True))

    @pytest.mark.parametrize("uploader", ["ImagineDragonsVEVO", "Imagine Dragons - Topic", "Sony Music Russia", "Queen Official"])
    def test_channels(self, uploader):
        assert R.is_official(T(uploader=uploader))

    def test_falls_back_to_artist_without_channel(self):
        assert R.is_official(T(artist="Something - Topic"))

    @pytest.mark.parametrize("uploader", ["7clouds", "Unofficial Uploads", "Pizza Music"])
    def test_not_official(self, uploader):
        assert not R.is_official(T(uploader=uploader))


# ── query analysis ─────────────────────────────────────────────────────────

class TestAnalyzeQuery:
    def test_explicit_split_is_kept(self):
        q = R.analyze_query("Imagine Dragons", "Believer", [])
        assert (q.artist, q.title, q.artist_mode) == ("Imagine Dragons", "Believer", False)

    def test_artist_without_title_is_artist_mode(self):
        q = R.analyze_query("Кино", "", [])
        assert q.artist_mode and q.artist == "Кино"

    def test_infers_artist_title_split(self):
        tracks = [T(artist="Кино", title="Группа крови"), T(artist="Кино, В. Цой", title="Группа крови (1988)"),
                  T(artist="Other", title="Song")]
        q = R.analyze_query("", "Кино Группа крови", tracks)
        assert (q.artist, q.title) == ("Кино", "Группа крови")

    def test_infers_reversed_order(self):
        tracks = [T(artist="Imagine Dragons", title="Believer")] * 2
        q = R.analyze_query("", "Believer Imagine Dragons", tracks)
        assert (q.artist, q.title) == ("Imagine Dragons", "Believer")

    def test_one_supporting_result_is_not_enough(self):
        q = R.analyze_query("", "Кино Группа крови", [T(artist="Кино", title="Группа крови")])
        assert (q.artist, q.title) == ("", "Кино Группа крови")

    def test_artist_only_query(self):
        tracks = [T(artist="Сломанный воздух", title=f"Song {i}") for i in range(3)] + [T(artist="X", title="Y")] * 9
        q = R.analyze_query("", "Сломанный воздух", tracks)
        assert q.artist_mode and q.artist == "Сломанный воздух" and q.title == ""

    def test_artist_share_needs_a_quarter_of_results(self):
        tracks = [T(artist="Сломанный воздух", title=f"Song {i}") for i in range(3)] + [T(artist="X", title="Y")] * 10
        assert not R.analyze_query("", "Сломанный воздух", tracks).artist_mode

    def test_versions_and_longform_from_raw_query(self):
        q = R.analyze_query("Imagine Dragons", "Believer (Live)", [])
        assert q.versions == {"live"} and not q.longform
        assert R.analyze_query("", "lofi mix", []).longform

    def test_raw(self):
        assert Q("A", "B").raw == "A B" and Q("", "B").raw == "B"


class TestArtistMatches:
    def test_words_subset(self):
        assert R.artist_matches(T(artist="Кино, В. Цой"), "Кино")
        assert not R.artist_matches(T(artist="Кинотеатр"), "Кино")

    def test_glued_channel_name(self):
        assert R.artist_matches(T(artist="ImagineDragons"), "Imagine Dragons")

    def test_empty_query_artist(self):
        assert not R.artist_matches(T(artist="A"), "")


# ── scoring ────────────────────────────────────────────────────────────────

class TestRelevance:
    def test_exact_match_is_max(self):
        assert R.text_relevance(T(artist="Imagine Dragons", title="Believer"), "Imagine Dragons", "Believer") == 1.0

    def test_word_order_tolerant(self):
        assert R.text_relevance(T(artist="Imagine Dragons", title="Believer"), "", "Believer Imagine Dragons") == 1.0

    def test_unrelated_is_low(self):
        assert R.text_relevance(T(artist="Metallica", title="One"), "Imagine Dragons", "Believer") < 0.4

    def test_title_only_similarity_counts(self):
        assert R.text_relevance(T(artist="Random Uploader", title="Believer"), "", "Believer") == 1.0

    def test_containment_bonus_only_for_whole_words(self):
        whole = R.text_relevance(T(artist="x", title="Believer extended radio edit"), "", "Believer")
        part = R.text_relevance(T(artist="x", title="Believers extended radio edit"), "", "Believer")
        assert whole > part

    def test_version_penalty_applies(self):
        orig, live = T(artist="A", title="Song"), T(artist="A", title="Song (Live)")
        assert R.relevance(orig, Q("A", "Song")) == 1.0
        assert R.relevance(live, Q("A", "Song")) == pytest.approx(1.0 - 0.35)

    def test_asked_version_not_penalised(self):
        live = T(artist="A", title="Song (Live)")
        assert R.relevance(live, Q("A", "Song (Live)", versions=frozenset({"live"}))) == 1.0

    def test_artist_mode(self):
        q = Q("Сломанный воздух", "", artist_mode=True)
        assert R.relevance(T(artist="Сломанный воздух", title="Аврора"), q) == 1.0
        other = R.relevance(T(artist="Юля Тепло", title="Сломанный воздух"), q)
        assert other == pytest.approx(0.85 * R.text_relevance(T(artist="Юля Тепло", title="Сломанный воздух"), "", "Сломанный воздух"))
        assert other < 1.0


class TestQualityScore:
    def test_lossless(self):
        assert R.quality_score(T(is_lossless=True, source="vk")) == 1.0

    def test_youtube_is_fixed(self):
        assert R.quality_score(T(source="youtube", bitrate=160)) == 0.75

    @pytest.mark.parametrize("kbps,expected", [(320, 1.0), (256, 0.8), (128, 0.4), (640, 1.0)])
    def test_measured_vk(self, kbps, expected):
        assert R.quality_score(T(source="vk", bitrate=kbps)) == pytest.approx(expected)

    def test_unmeasured_defaults(self):
        assert R.quality_score(T(source="vk")) == 0.8
        assert R.quality_score(T(source="spotify")) == 0.7

    def test_derived_from_size(self):
        # 4_000_000 B * 8 / 200 s / 1000 = 160 kbps
        assert R.quality_score(T(source="vk", duration=200, filesize=4_000_000)) == pytest.approx(0.5)

    def test_size_needs_duration(self):
        assert R.quality_score(T(source="vk", duration=0, filesize=4_000_000)) == 0.8


class TestDurationFit:
    @pytest.mark.parametrize("d,ref,expected", [
        (200, 200, 1.0), (206, 200, 1.0), (194, 200, 1.0),          # ±3%: same recording
        (230, 200, pytest.approx(1 - 4 * 0.12)),                    # +15%: longer drops fast
        (170, 200, pytest.approx(1 - 3 * 0.12)),                    # −15%: shorter drops slower
        (400, 200, 0.0), (0, 200, 0.5), (200, 0, 0.5),
    ])
    def test_values(self, d, ref, expected):
        assert R.duration_fit(d, ref) == expected

    def test_concert_length_scores_below_radio_edit(self):
        assert R.duration_fit(240, 200) < R.duration_fit(160, 200)


class TestRankSignal:
    def test_values(self):
        assert R._rank_signal(T(source_rank=0)) == 1.0
        assert R._rank_signal(T(source_rank=10)) == 0.5
        assert R._rank_signal(T()) == 0.0


# ── grouping ───────────────────────────────────────────────────────────────

def groups(tracks):
    return [[e[0] for e in g.members] for g in R._group([(t, 1.0, 0.0, i) for i, t in enumerate(tracks)])]


class TestGrouping:
    def test_tokens_drop_noise_and_add_glued_artist(self):
        t = T(artist="Imagine Dragons", title="Believer (Official Music Video) HD")
        assert R.track_tokens(t) == {"imagine", "dragons", "imaginedragons", "believer"}

    def test_same_song_across_sources(self):
        a = T(artist="Imagine Dragons", title="Believer", duration=204)
        b = T(artist="ImagineDragons", title="Believer (Audio)", duration=203, source="vk")
        assert groups([a, b]) == [[a, b]]

    def test_longer_title_is_another_song(self):
        a = T(artist="Земфира", title="Хочешь", duration=215)
        b = T(artist="Земфира", title="Хочешь сладких апельсинов?", duration=207)
        assert groups([a, b]) == [[a], [b]]

    def test_cover_artist_is_another_song(self):
        a = T(artist="Metallica", title="Nothing Else Matters", duration=367)
        b = T(artist="Radio Tapok", title="Nothing Else Matters (Metallica)", duration=370)
        assert groups([a, b]) == [[a], [b]]

    def test_versions_split(self):
        a = T(artist="A", title="Song", duration=200)
        b = T(artist="A", title="Song (Slowed)", duration=210)
        assert groups([a, b]) == [[a], [b]]

    def test_length_gate(self):
        a, b = T(artist="A", title="Song", duration=200), T(artist="A", title="Song", duration=300)
        assert groups([a, b]) == [[a], [b]]
        c = T(artist="A", title="Song", duration=260)  # exactly +30%
        assert groups([a, c]) == [[a, c]]

    @pytest.mark.parametrize("d1,d2", [(0, 200), (200, 0)])
    def test_unknown_length_merges(self, d1, d2):
        a, b = T(artist="A", title="Song", duration=d1), T(artist="A", title="Song", duration=d2, source="vk")
        assert groups([a, b]) == [[a, b]]

    def test_tokenless_track_never_merges(self):
        a, b = T(artist="", title="the official video"), T(artist="", title="the official video", source="vk")
        assert groups([a, b]) == [[a], [b]]

    def test_missing_artist_does_not_block(self):
        a, b = T(artist="", title="Believer Imagine Dragons"), T(artist="Imagine Dragons", title="Believer", source="vk")
        assert groups([a, b]) == [[a, b]]


class TestRecordings:
    def entries(self, *durations):
        return [(T(artist="A", title="S", duration=d, source_id=str(i)), 1.0, 0.0, i) for i, d in enumerate(durations)]

    def test_split_by_eight_percent(self):
        recs = R._split_recordings(self.entries(286, 239, 284, 240, 235))
        assert [[e[0].duration for e in r] for r in recs] == [[235, 239, 240], [284, 286]]

    def test_unknown_lengths_join_the_biggest(self):
        recs = R._split_recordings(self.entries(0, 200, 300, 301))
        assert [[e[0].duration for e in r] for r in recs] == [[200], [300, 301, 0]]

    def test_all_unknown(self):
        assert len(R._split_recordings(self.entries(0, 0))) == 1

    def test_reference_prefers_studio_copies(self):
        studio = (T(source="vk", official=True, duration=204), 1.0, 0.0, 0)
        video = (T(uploader="ImagineDragons", official=True, title="Believer (Official Video)", duration=217), 1.0, 0.0, 1)
        lyrics = (T(title="Believer (Lyrics)", duration=180), 1.0, 0.0, 2)
        assert R._reference_length([studio, video, lyrics]) == 204
        assert R._reference_length([video, lyrics]) == 217
        assert R._reference_length([lyrics]) == 180
        assert R._reference_length([(T(duration=0), 1.0, 0.0, 0)]) == 0.0

    def test_studio_copy_kinds(self):
        assert R._is_studio_copy(T(uploader="Queen - Topic", title="Bohemian Rhapsody"))
        assert R._is_studio_copy(T(official=True, title="Believer (Official Audio)"))
        assert not R._is_studio_copy(T(official=True, title="Believer (Official Audio Video)"))
        assert not R._is_studio_copy(T(source="vk"))
        assert not R._is_studio_copy(T(source="vk", official=True, title="Song (Lyrics)"))


# ── the whole order ────────────────────────────────────────────────────────

class TestRank:
    def test_best_quality_copy_of_the_recording_first(self):
        yt = T(artist="Кино", title="Группа крови", duration=285, official=True)
        vk128 = T(artist="Кино", title="Группа крови", duration=284, source="vk", bitrate=128)
        vk320 = T(artist="Кино", title="Группа крови", duration=286, source="vk", bitrate=320)
        assert R.rank([yt, vk128, vk320], Q("Кино", "Группа крови")) == [vk320, yt, vk128]

    def test_concert_recording_after_the_studio_one(self):
        studio = T(artist="Rammstein", title="Sonne", duration=272, source="vk", bitrate=320, official=True)
        concert = T(artist="Rammstein", title="Sonne", duration=320, source="vk", bitrate=320)  # +18%, untagged
        assert R.rank([concert, studio], Q("Rammstein", "Sonne")) == [studio, concert]

    def test_tagged_versions_sink_below_original(self):
        orig = T(artist="Imagine Dragons", title="Believer", duration=204)
        tagged = [T(artist="Imagine Dragons", title=f"Believer ({v})", duration=204, source="vk", bitrate=320)
                  for v in ("Live", "Karaoke", "cover", "Slowed + Reverb", "Remix")]
        assert R.rank(tagged + [orig], Q("Imagine Dragons", "Believer"))[0] is orig

    def test_popular_song_wins_a_title_only_query(self):
        hit = [T(artist="Imagine Dragons", title="Believer", duration=204, views=3_000_000_000, source_id=f"h{i}")
               for i in range(5)]
        other = T(artist="No Resolve", title="Believer", duration=206, source="vk", bitrate=320, source_rank=0)
        assert R.rank([other] + hit, Q("", "Believer"))[0].artist == "Imagine Dragons"

    def test_artist_mode_orders_songs_by_popularity(self):
        rare = T(artist="Band", title="B-side", source="vk", source_rank=30)
        hit = [T(artist="Band", title="Hit", source="vk", source_rank=i, source_id=f"h{i}") for i in range(3)]
        out = R.rank([rare] + hit, Q("Band", "", artist_mode=True))
        assert out[-1] is rare

    def test_official_beats_lyrics_reupload_in_a_recording(self):
        lyrics = T(artist="Imagine Dragons", title="Believer (Lyrics)", uploader="7clouds", duration=204)
        official = T(artist="Imagine Dragons", title="Believer (Audio)", uploader="ImagineDragons", official=True, duration=203)
        assert R.rank([lyrics, official], Q("Imagine Dragons", "Believer"))[0] is official

    def test_junk_last(self):
        junk = T(artist="Imagine Dragons", title="Believer podcast episode", duration=3600)
        exact = T(artist="Imagine Dragons", title="Believer", duration=204)
        out = R.rank([junk, exact], Q("Imagine Dragons", "Believer"))
        assert out == [exact, junk]

    def test_min_score(self):
        good, bad = T(artist="Imagine Dragons", title="Believer"), T(artist="Metallica", title="One")
        assert R.rank([good, bad], Q("Imagine Dragons", "Believer"), min_score=0.6) == [good]
        assert R.rank([bad], Q("Imagine Dragons", "Believer"), min_score=0.99) == []

    def test_score_equal_to_min_is_kept(self):
        t = T(artist="A", title="Song")
        assert R.rank([t], Q("A", "Song"), min_score=R.relevance(t, Q("A", "Song"))) == [t]

    def test_nothing_dropped_and_ties_keep_order(self):
        tracks = [T(artist=f"a{i}", title=f"t{i}") for i in range(6)]
        out = R.rank(tracks, Q("", "zzz"))
        assert sorted(map(id, out)) == sorted(map(id, tracks))
        a, b = T(artist="A", title="Song", source_id="a"), T(artist="A", title="Song", source_id="b")
        assert R.rank([a, b], Q("A", "Song")) == [a, b]
        assert R.rank([b, a], Q("A", "Song")) == [b, a]

    def test_heavier_file_breaks_ties(self):
        light = T(artist="A", title="Song", source="vk", filesize=1_000, source_id="l")
        heavy = T(artist="A", title="Song", source="vk", filesize=1_200, source_id="h")
        # 1_000 B / 200 s vs 1_200 B / 200 s: both near 0 kbps → quality tie
        assert R.rank([light, heavy], Q("A", "Song"))[0] is heavy

    def test_rank_by_relevance_analyses_the_query(self):
        a = T(artist="Кино", title="Группа крови", duration=285)
        b = T(artist="Кино", title="Группа крови", duration=284, source="vk")
        c = T(artist="Ария", title="Кровь", duration=200)
        assert R.rank_by_relevance([c, a, b], "", "Кино Группа крови")[-1] is c

    def test_empty(self):
        assert R.rank([], Q("A", "B")) == []


class TestBestMatch:
    def test_studio_length_official_over_video_and_live(self):
        spotify = T(artist="The Weeknd", title="Blinding Lights", duration=200, source="spotify", official=True)
        video = T(artist="The Weeknd", title="Blinding Lights (Official Video)", duration=263, official=True)
        live = T(artist="The Weeknd", title="Blinding Lights (Live)", duration=205)
        audio = T(artist="The Weeknd", title="Blinding Lights (Official Audio)", duration=201, official=True)
        lyrics = T(artist="The Weeknd", title="Blinding Lights (Lyrics)", duration=199)
        assert R.best_match(spotify, [video, live, lyrics, audio]) is audio

    def test_none_for_no_candidates(self):
        assert R.best_match(T(), []) is None


# ── real search results (collected from prod 2026-11-08) ──────────────────

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "search_samples.json").read_text())


def load_sample(query: str) -> list[TrackInfo]:
    """Build tracks exactly as services/youtube.py and vk_music.py do."""
    d = SAMPLES[query]
    tracks = []
    for i, r in enumerate(d["youtube"]):
        uploader = r["uploader"] or ""
        artist, title = split_video_title(r["title"] or "", uploader)
        tracks.append(TrackInfo(
            title=title, artist=artist, duration=int(r["duration"] or 0), source="youtube", source_id=f"y{i}",
            bitrate=160, uploader=uploader, views=int(r["view_count"] or 0), source_rank=i,
            official=is_official_upload(uploader, artist, r["title"] or "", bool(r["channel_is_verified"])),
        ))
    for i, r in enumerate(d["vk"]):
        tracks.append(TrackInfo(
            title=r["title"], artist=r["artist"], duration=int(r["duration"]), source="vk", source_id=f"v{i}",
            bitrate=nominal_kbps(r["kbps"]), official=r["is_official"], source_rank=r["rank"],
        ))
    return tracks


def ranked(query: str) -> list[TrackInfo]:
    return R.rank_by_relevance(load_sample(query), "", query)


class TestRealResults:
    @pytest.mark.parametrize("query", sorted(SAMPLES))
    def test_no_unasked_version_in_top_five(self, query):
        for t in ranked(query)[:5]:
            assert not R.track_versions(t), f"{t.artist} – {t.title}"

    def test_kino_album_version_in_320_first(self):
        top = ranked("Кино Группа крови")[:3]
        assert all(t.source == "vk" and t.bitrate == 320 and "Кино" in t.artist and 280 <= t.duration <= 290
                   for t in top)

    def test_karaoke_no_longer_first(self):
        top = ranked("Король и Шут Кукла колдуна")
        assert top[0].source == "vk" and top[0].bitrate == 320
        assert all("karaoke" not in R.track_versions(t) for t in top[:10])

    @pytest.mark.parametrize("query", ["Believer", "Imagine Dragons Believer"])
    def test_believer_is_imagine_dragons(self, query):
        assert all(t.artist == "Imagine Dragons" for t in ranked(query)[:5])

    def test_artist_query_lists_the_band(self):
        tracks = load_sample("Сломанный воздух")
        assert R.analyze_query("", "Сломанный воздух", tracks).artist_mode
        assert all(R.artist_matches(t, "Сломанный воздух") for t in ranked("Сломанный воздух")[:10])

    def test_cover_by_another_artist_not_on_top(self):
        assert all(t.artist == "Metallica" for t in ranked("Metallica Nothing Else Matters")[:5])

    def test_long_video_cut_below_studio_copies(self):
        top = ranked("The Weeknd Blinding Lights")[:5]
        assert all(t.duration < 230 for t in top)

    def test_queen_official_320_first(self):
        first = ranked("Queen Bohemian Rhapsody")[0]
        assert first.source == "vk" and first.bitrate == 320 and first.official

    def test_different_song_with_longer_title_not_on_top(self):
        assert all("апельсин" not in t.title.lower() for t in ranked("Земфира Хочешь")[:10])

    def test_concerts_sink(self):
        for query in ("Кино Группа крови", "Земфира Хочешь", "Rammstein Sonne"):
            out = ranked(query)
            live = [i for i, t in enumerate(out) if "live" in R.track_versions(t)]
            assert live and min(live) >= 10, query


class TestNegativeRelevance:
    def test_kept_without_threshold(self):
        # unrelated artist + karaoke: relevance below zero, still listed (last)
        t = T(artist="Someone", title="Other (Karaoke)")
        good = T(artist="Imagine Dragons", title="Believer")
        assert R.relevance(t, Q("Imagine Dragons", "Believer")) < 0
        assert R.rank([t, good], Q("Imagine Dragons", "Believer")) == [good, t]


# ── exact values and boundaries (written against surviving mutants) ────────

from difflib import SequenceMatcher  # noqa: E402


class TestExactRelevance:
    def test_combined_similarity_can_be_the_best_term(self):
        t = T(artist="Imagine Dragons", title="Believer")
        expected = SequenceMatcher(None, "imagin dragons belever", "imagine dragons believer").ratio()
        assert R.text_relevance(t, "", "Imagin Dragons Belever") == pytest.approx(expected)

    def test_with_artist_the_title_decides(self):
        t = T(artist="Imagine Dragons", title="Believer")
        title_sim = SequenceMatcher(None, "belever", "believer").ratio()
        artist_sim = SequenceMatcher(None, "imagin dragons", "imagine dragons").ratio()
        assert R.text_relevance(t, "Imagin Dragons", "Belever") == pytest.approx(title_sim * (0.7 + 0.3 * artist_sim))

    def test_another_song_of_the_artist_is_not_a_match(self):
        thunder = T(artist="Imagine Dragons", title="Thunder")
        assert R.text_relevance(thunder, "Imagine Dragons", "Believer") < 0.5

    def test_title_word_overlap_counts_with_artist(self):
        t = T(artist="Imagine Dragons", title="Believer Thunder")
        assert R.text_relevance(t, "Imagine Dragons", "Thunder Believer") == 1.0

    def test_artist_in_title_when_track_has_no_artist(self):
        t = T(artist="", title="Imagine Dragons Believer")
        expected = SequenceMatcher(None, "believer", "imagine dragons believer").ratio() * (0.7 + 0.3 * 0.0)
        # + title containment, + artist containment ("imagine dragons" in the title)
        assert R.text_relevance(t, "Imagine Dragons", "Believer") == pytest.approx(min(1.0, expected + 0.24))

    def test_empty_titles_are_not_a_match(self):
        assert R.text_relevance(T(artist="X", title=""), "Y", "") == 0.0
        assert R.text_relevance(T(artist="Metallica", title="One"), "Queen", "") < 0.5

    def test_title_containment_bonus_value(self):
        t = T(artist="x", title="Believer extended radio edit version")
        q, tt = "believer", "believer extended radio edit version"
        base = max(SequenceMatcher(None, q, tt).ratio(), SequenceMatcher(None, q, "x " + tt).ratio(),
                   R._token_set_ratio(q, "x " + tt))
        assert R.text_relevance(t, "", "Believer") == pytest.approx(base + 0.12)

    def test_artist_containment_bonus_value(self):
        t = T(artist="Imagine Dragons", title="Completely Different Words Here")
        base = SequenceMatcher(None, "zzz", "completely different words here").ratio()
        assert R.text_relevance(t, "Imagine Dragons", "zzz") == pytest.approx(base * 1.0 + 0.12)

    def test_artist_mismatch_penalty_value(self):
        # title 1.0 × (0.7 + 0.3 × artist 0.0) + title containment 0.12, then the mismatch penalty
        assert R.relevance(T(artist="Other", title="Song"), Q("A", "Song")) == pytest.approx(0.7 + 0.12 - 0.2)


class TestExactHelpers:
    def test_artist_match_glued_both_ways_and_length(self):
        assert R.artist_matches(T(artist="Imagine Dragons"), "ImagineDragons")
        assert R.artist_matches(T(artist="ABC"), "A BC")          # glued name of exactly 3 letters
        assert not R.artist_matches(T(artist="AB"), "A B")

    def test_infer_split_needs_artist_and_title(self):
        tracks = [T(artist="Beta", title="x"), T(artist="Beta", title="y")]
        assert R._infer_split("Alpha Beta", tracks) == ("", "Alpha Beta")

    def test_token_boundaries(self):
        assert R.track_tokens(T(artist="AC DC", title="x")) == {"ac", "dc", "acdc"}
        assert R.artist_words(T(artist="A BC DEF")) == {"bc", "def"}
        assert R.title_tokens(T(title="Ab Believer Official Video x")) == {"ab", "believer"}

    def test_overlap_with_an_empty_side(self):
        assert R._overlap(set(), {"a"}, by_larger=False) == 0.0
        assert R._overlap({"a"}, set(), by_larger=True) == 0.0
        assert R._overlap({"a", "b"}, {"a"}, by_larger=False) == 1.0
        assert R._overlap({"a", "b"}, {"a"}, by_larger=True) == 0.5

    def test_same_artist_rules(self):
        kino = T(artist="Кино")
        assert R._same_artist(T(artist="Кино Цой Игорь"), {"кино", "цой", "игорь"}, kino, {"кино"})
        a, b = T(artist="Imagine Dragons"), T(artist="ImagineDragons")
        assert R._same_artist(a, R.artist_words(a), b, R.artist_words(b))
        c, d = T(artist="A BC"), T(artist="ABC")                  # glued "abc": exactly 3 letters
        assert R._same_artist(c, R.artist_words(c), d, R.artist_words(d))
        g, h = T(artist="Xy"), T(artist="Zw")
        assert not R._same_artist(g, R.artist_words(g), h, R.artist_words(h))

    def test_official_topic_without_dash(self):
        assert R.is_official(T(uploader="ImagineDragonsTopic"))

    def test_spotify_official_is_a_studio_copy(self):
        assert R._is_studio_copy(T(source="spotify", official=True))
        assert not R._is_studio_copy(T(source="spotify"))

    def test_junk_tag_and_length_add_up(self):
        assert R.junk_penalty(T(title="Interview", duration=1500), "x", False) == pytest.approx(2.6)
        assert R.junk_penalty(T(title="Greatest Hits", duration=700), "x", False) == pytest.approx(1.2)


class TestExactQualityAndLength:
    def test_tiny_measured_bitrate_is_used(self):
        assert R.quality_score(T(source="vk", bitrate=1)) == pytest.approx(1 / 320)
        assert R.quality_score(T(source="vk", bitrate=1, duration=200, filesize=8_000_000)) == pytest.approx(1 / 320)

    def test_size_derivation_edges(self):
        assert R.quality_score(T(source="vk", duration=100, filesize=1)) == pytest.approx(8e-5 / 320, abs=1e-9)
        assert R.quality_score(T(source="vk", duration=1, filesize=40_000)) == 1.0

    def test_one_second_lengths(self):
        assert R.duration_fit(1, 1.0) == 1.0

    def test_median_length_ignores_unknown(self):
        e = lambda d: (T(duration=d), 1.0, 0.0, 0)  # noqa: E731
        assert R._median_length([e(0), e(1), e(3)]) == 2.0
        assert R._median_length([]) == 0.0

    def test_reference_ignores_unknown(self):
        e = lambda d: (T(source="vk", official=True, duration=d), 1.0, 0.0, 0)  # noqa: E731
        assert R._reference_length([e(0), e(1), e(3)]) == 2.0

    def test_split_keeps_one_second_tracks_once(self):
        recs = R._split_recordings([(T(duration=1), 1.0, 0.0, 0)])
        assert [[e[0].duration for e in r] for r in recs] == [[1]]

    def test_split_exactly_eight_percent_is_one_recording(self):
        entries = [(T(duration=d, source_id=str(d)), 1.0, 0.0, i) for i, d in enumerate((25, 27))]
        assert len(R._split_recordings(entries)) == 1

    def test_all_unknown_kept(self):
        entries = [(T(duration=0, source_id=str(i)), 1.0, 0.0, i) for i in range(2)]
        assert R._split_recordings(entries) == [entries]


class TestExactPopularity:
    def members(self, n, **kw):
        return [(T(source_id=f"m{i}", **kw), 1.0, 0.0, i) for i in range(n)]

    def test_size_term(self):
        import math
        assert R._popularity(self.members(3), 0) == pytest.approx(0.4 * math.log1p(3) / math.log1p(12))

    def test_size_capped(self):
        assert R._popularity(self.members(20), 0) == pytest.approx(0.4)

    def test_one_view_is_the_maximum(self):
        assert R._popularity(self.members(20, views=1), 1) == pytest.approx(0.8)


class TestExactOrder:
    def test_better_source_rank_breaks_copy_ties(self):
        late = T(source="vk", bitrate=320, source_rank=20, source_id="late")
        first = T(source="vk", bitrate=320, source_rank=0, source_id="first")
        assert R.rank([late, first], Q("Artist", "Song")) == [first, late]

    def test_length_closer_to_the_recording_first(self):
        on = T(source="vk", bitrate=320, duration=200, source_id="on")
        off = T(source="vk", bitrate=320, duration=214, source_id="off")      # +7%: same recording
        mid = T(source="vk", bitrate=320, duration=200, source_id="mid")
        assert R.rank([off, on, mid], Q("Artist", "Song")) == [on, mid, off]

    def test_quality_beats_a_small_length_difference(self):
        hq = T(source="vk", bitrate=320, duration=214, source_id="hq")       # +7% vs the 200 s copies
        lq = T(source="vk", bitrate=256, duration=200, source_id="lq")
        lq2 = T(source="vk", bitrate=256, duration=200, source_id="lq2")
        assert R.rank([lq, lq2, hq], Q("Artist", "Song"))[0] is hq

    def test_lyric_reupload_in_320_still_beats_plain_youtube(self):
        lyr = T(title="Song (Lyrics)", source="vk", bitrate=320, source_id="lyr")
        yt = T(title="Song", source="youtube", source_id="yt")
        assert R.rank([yt, lyr], Q("Artist", "Song"))[0] is lyr

    def test_heavier_youtube_copy_first(self):
        light = T(source="youtube", filesize=1_000, source_id="l")
        heavy = T(source="youtube", filesize=2_000, source_id="h")
        assert R.rank([light, heavy], Q("Artist", "Song")) == [heavy, light]

    def test_official_recording_before_a_more_copied_one(self):
        a = (T(official=True, duration=200, source_id="a"), 1.0, 0.0, 0)
        bs = [(T(duration=230, source_id=f"b{i}"), 1.0, 0.0, i + 1) for i in range(5)]
        assert R._order_song([a] + bs, 0)[0].source_id == "a"

    def test_official_song_before_equal_other(self):
        plain = T(artist="B", title="Song", source="vk", bitrate=320)
        official = T(artist="A", title="Song", source="vk", bitrate=320, official=True)
        assert R.rank([plain, official], Q("", "Song"))[0] is official

    def test_better_quality_song_before_equal_other(self):
        low = T(artist="B", title="Song", source="vk", bitrate=128)
        high = T(artist="A", title="Song", source="vk", bitrate=320)
        assert R.rank([low, high], Q("", "Song"))[0] is high

    def test_longform_query_does_not_sink_long_items(self):
        mix = T(artist="Chill", title="Lofi mix podcast", duration=7200)
        short = T(artist="Other", title="Lofi", duration=200)
        out = R.rank([short, mix], R.analyze_query("", "lofi mix podcast", [short, mix]))
        assert out[0] is mix

    def test_rank_by_relevance_passes_artist_and_threshold(self):
        good = T(artist="Imagine Dragons", title="Believer")
        cover = T(artist="Someone", title="Believer")
        assert R.rank_by_relevance([cover, good], "Imagine Dragons", "Believer", min_score=0.9) == [good]


class TestExactBestMatch:
    def test_needs_artist_title_and_version(self):
        ref = T(artist="Imagine Dragons", title="Believer", duration=204, source="spotify", official=True)
        other_artist = T(artist="Someone", title="Believer", duration=204, official=True)
        assert R.best_match(ref, [other_artist, T(artist="Imagine Dragons", title="Believer", duration=204)]).artist == \
            "Imagine Dragons"
        other_song = T(artist="Imagine Dragons", title="Thunder", duration=204, official=True)
        assert R.best_match(ref, [other_song, T(artist="Imagine Dragons", title="Believer", duration=230)]).title == \
            "Believer"
        live_ref = T(artist="A", title="Song (Live)", duration=200, source="spotify")
        studio, live = T(artist="A", title="Song", duration=200), T(artist="A", title="Song (Live)", duration=200)
        assert R.best_match(live_ref, [studio, live]) is live

    def test_weights(self):
        ref = T(artist="A", title="Song", duration=200, source="spotify")
        # relevance 1.0 each; length +12% costs 0.5*(1-4*0.09)... vs official 0.1
        long_official = T(artist="A", title="Song", duration=224, official=True)
        exact = T(artist="A", title="Song", duration=200)
        assert R.best_match(ref, [long_official, exact]) is exact
        near_official = T(artist="A", title="Song", duration=207, official=True)   # +3.5%: tiny length cost
        assert R.best_match(ref, [exact, near_official]) is near_official
        title_off = T(artist="A", title="Song Remastered Edition", duration=200)
        far = T(artist="A", title="Song", duration=300)
        assert R.best_match(ref, [far, title_off]) is title_off
