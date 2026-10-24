"""Search ranking: normalisation, relevance, quality, junk sinking, grouping."""

import pytest

from bot.handlers import search as s


class TestNormalize:
    def test_strips_brackets_punctuation_and_case(self):
        assert s._normalize("Believer (Official Video) [HD] - Imagine!") == "believer imagine"

    def test_collapses_whitespace(self):
        assert s._normalize("  a   b\tc ") == "a b c"

    def test_keeps_cyrillic_words(self):
        assert s._normalize("Кино — Группа крови") == "кино группа крови"


class TestLongformQuery:
    @pytest.mark.parametrize("q", ["lofi hip hop", "Подкаст про музыку", "full album", "2 часа музыки"])
    def test_longform(self, q):
        assert s._is_longform_query(q)

    @pytest.mark.parametrize("q", ["Imagine Dragons Believer", "Кино Группа крови"])
    def test_song(self, q):
        assert not s._is_longform_query(q)


class TestTokenSetRatio:
    def test_order_independent(self):
        assert s._token_set_ratio("believer imagine dragons", "imagine dragons believer") == 1.0

    def test_partial_overlap_divides_by_larger_set(self):
        assert s._token_set_ratio("a b", "a b c d") == 0.5

    def test_empty_side_is_zero(self):
        assert s._token_set_ratio("", "a") == 0.0
        assert s._token_set_ratio("a", "") == 0.0


class TestJunkPenalty:
    def test_longform_query_disables_penalty(self, make_track):
        t = make_track(title="Podcast episode 5", duration=4000)
        assert s._junk_penalty(t, "podcast", longform=True) == 0.0

    def test_hard_junk_tag(self, make_track):
        assert s._junk_penalty(make_track(title="Interview with X", duration=200), "x", False) == 2.0

    def test_soft_junk_tag(self, make_track):
        assert s._junk_penalty(make_track(title="Greatest Hits", duration=200), "x", False) == 1.0

    def test_tag_present_in_query_is_not_penalised(self, make_track):
        assert s._junk_penalty(make_track(title="Greatest Hits", duration=200), "greatest hits", False) == 0.0

    @pytest.mark.parametrize("dur,expected", [
        (600, 0.0), (601, 0.2), (1200, 0.2), (1201, 0.6), (1800, 0.6), (1801, 1.5),
    ])
    def test_duration_thresholds(self, make_track, dur, expected):
        assert s._junk_penalty(make_track(title="Song", duration=dur), "song", False) == pytest.approx(expected)

    def test_hard_junk_and_overlong_stack(self, make_track):
        t = make_track(title="Podcast", duration=4000)
        assert s._junk_penalty(t, "x", False) == pytest.approx(3.5)


class TestOfficial:
    @pytest.mark.parametrize("artist", ["ImagineDragonsVEVO", "Imagine Dragons - Topic", "Sony Music Russia", "Official Channel"])
    def test_official(self, make_track, artist):
        assert s._is_official(make_track(artist=artist))

    def test_not_official(self, make_track):
        assert not s._is_official(make_track(artist="7clouds"))

    def test_boost_values(self, make_track):
        assert s._official_boost(make_track(artist="x", title="y")) == 0.0
        assert s._official_boost(make_track(artist="SomeVEVO", title="y")) == pytest.approx(0.20)
        assert s._official_boost(make_track(artist="x", title="y (Official Video)")) == pytest.approx(0.08)
        assert s._official_boost(make_track(artist="SomeVEVO", title="Official")) == pytest.approx(0.28)


class TestRelevanceScore:
    def test_exact_match_is_max(self, make_track):
        t = make_track(artist="Imagine Dragons", title="Believer")
        assert s.relevance_score(t, "Imagine Dragons", "Believer") == 1.0

    def test_word_order_tolerant(self, make_track):
        t = make_track(artist="Imagine Dragons", title="Believer")
        assert s.relevance_score(t, "", "Believer Imagine Dragons") == 1.0

    def test_unrelated_is_low(self, make_track):
        t = make_track(artist="Metallica", title="One")
        assert s.relevance_score(t, "Imagine Dragons", "Believer") < 0.4

    def test_remix_penalised_unless_asked(self, make_track):
        orig = make_track(artist="Imagine Dragons", title="Believer")
        remix = make_track(artist="Imagine Dragons", title="Believer (Slowed + Reverb)")
        remix_plain = make_track(artist="Imagine Dragons", title="Believer slowed")
        assert s.relevance_score(remix_plain, "Imagine Dragons", "Believer") < s.relevance_score(orig, "Imagine Dragons", "Believer")
        # bracketed tags are stripped by normalisation, so they don't count as edits
        assert s.relevance_score(remix, "Imagine Dragons", "Believer") == 1.0
        assert s.relevance_score(remix_plain, "Imagine Dragons", "Believer slowed") == 1.0

    def test_containment_bonus(self, make_track):
        base = make_track(artist="Other", title="Believer extended version here")
        assert s.relevance_score(base, "", "Believer") >= 0.12

    def test_clamped_to_unit_interval(self, make_track):
        t = make_track(artist="zzz", title="qqq slowed")
        assert 0.0 <= s.relevance_score(t, "a", "b") <= 1.0


class TestQualityScore:
    def test_lossless_is_one(self, make_track):
        assert s.quality_score(make_track(is_lossless=True)) == 1.0

    def test_known_bitrate_scaled_to_320(self, make_track):
        assert s.quality_score(make_track(bitrate=160)) == pytest.approx(0.5)

    def test_capped_at_one(self, make_track):
        assert s.quality_score(make_track(bitrate=640)) == 1.0

    def test_derived_from_size(self, make_track):
        # 4_000_000 B * 8 / 200 s / 1000 = 160 kbps → 0.5
        assert s.quality_score(make_track(duration=200, filesize=4_000_000)) == pytest.approx(0.5)

    def test_unknown_defaults_by_source(self, make_track):
        assert s.quality_score(make_track(source="vk")) == 1.0
        assert s.quality_score(make_track(source="youtube")) == pytest.approx(0.6)


class TestMedianAndDuration:
    @pytest.mark.parametrize("values,expected", [([], 0.0), ([3.0], 3.0), ([1.0, 3.0], 2.0), ([5.0, 1.0, 3.0], 3.0)])
    def test_median(self, values, expected):
        assert s._median(values) == expected

    def test_duration_score(self):
        assert s._duration_score(200, 200.0) == 1.0
        assert s._duration_score(300, 200.0) == pytest.approx(0.5)
        assert s._duration_score(1000, 200.0) == 0.0
        assert s._duration_score(0, 200.0) == 0.5
        assert s._duration_score(200, 0.0) == 0.5


class TestGrouping:
    def test_tokens_drop_noise_words(self, make_track):
        t = make_track(artist="ImagineDragonsVEVO", title="Believer (Official Music Video) HD")
        assert s._track_tokens(t) == {"imaginedragonsvevo", "believer", "hd"} - {"hd"}

    def test_same_song_across_sources_grouped_best_quality_first(self, make_track):
        yt = make_track(artist="Imagine Dragons", title="Believer", source="youtube", duration=204)
        other = make_track(artist="Metallica", title="One", source="youtube", duration=446)
        vk = make_track(artist="Imagine Dragons", title="Believer", source="vk", bitrate=320, duration=205)
        out = s._group_same_track([yt, other, vk])
        assert out == [vk, yt, other]

    def test_duration_gate_keeps_different_songs_apart(self, make_track):
        a = make_track(artist="Imagine Dragons", title="Believer", duration=200)
        b = make_track(artist="Imagine Dragons", title="Believer", duration=400)
        assert s._group_same_track([a, b]) == [a, b]

    def test_nothing_is_removed(self, make_track):
        tracks = [make_track(title=f"t{i}", artist=f"a{i}") for i in range(5)]
        assert sorted(map(id, s._group_same_track(tracks))) == sorted(map(id, tracks))


class TestRankByRelevance:
    def test_exact_match_first_and_junk_last(self, make_track):
        junk = make_track(artist="Imagine Dragons", title="Believer podcast episode", duration=3600)
        cover = make_track(artist="Some Covers", title="Believer cover", duration=200)
        exact = make_track(artist="Imagine Dragons", title="Believer", duration=204)
        out = s.rank_by_relevance([junk, cover, exact], "Imagine Dragons", "Believer")
        assert out[0] is exact
        assert out[-1] is junk

    def test_min_score_filters(self, make_track):
        good = make_track(artist="Imagine Dragons", title="Believer")
        bad = make_track(artist="Metallica", title="One")
        assert s.rank_by_relevance([good, bad], "Imagine Dragons", "Believer", min_score=0.6) == [good]

    def test_all_filtered_returns_empty(self, make_track):
        assert s.rank_by_relevance([make_track(artist="x", title="y")], "Imagine Dragons", "Believer", min_score=0.99) == []

    def test_official_beats_lyrics_channel(self, make_track):
        lyrics = make_track(artist="7clouds", title="Imagine Dragons - Believer (Lyrics)", duration=204)
        official = make_track(artist="ImagineDragonsVEVO", title="Imagine Dragons - Believer (Official Music Video)", duration=204)
        out = s.rank_by_relevance([lyrics, official], "Imagine Dragons", "Believer")
        assert out[0] is official

    def test_consensus_duration_wins_between_equal_matches(self, make_track):
        a = make_track(artist="A", title="Song", source_id="a", duration=200)
        b = make_track(artist="A", title="Song", source_id="b", duration=250)
        c = make_track(artist="A", title="Song", source_id="c", duration=300)
        # Reference length is the median (250s), so b is the "release" version
        out = s.rank_by_relevance([a, c, b], "A", "Song")
        assert out[0] is b

    def test_original_order_breaks_full_ties(self, make_track):
        a = make_track(artist="A", title="Song", source_id="a")
        b = make_track(artist="A", title="Song", source_id="b")
        assert s.rank_by_relevance([a, b], "A", "Song") == [a, b]
        assert s.rank_by_relevance([b, a], "A", "Song") == [b, a]

    def test_heavier_file_breaks_score_ties(self, make_track):
        light = make_track(artist="A", title="Song", source="vk", source_id="l", filesize=1_000)
        heavy = make_track(artist="A", title="Song", source="vk", source_id="h", filesize=2_000)
        # both 0 bitrate on vk → same quality score; filesize tie-break puts heavy first
        assert s.rank_by_relevance([light, heavy], "A", "Song")[0] is heavy


class TestSortAndFilter:
    def test_bitrate_sorts(self, make_track):
        lo, hi = make_track(bitrate=128), make_track(bitrate=320)
        assert s.sort_tracks([lo, hi], "bitrate_desc") == [hi, lo]
        assert s.sort_tracks([hi, lo], "bitrate_asc") == [lo, hi]

    def test_title_sort_is_case_insensitive(self, make_track):
        b, a = make_track(title="beta"), make_track(title="Alpha")
        assert s.sort_tracks([b, a], "title") == [a, b]
        assert s.sort_tracks([b, a], "asc") == [a, b]

    def test_default_sort_duration_then_bigger_first(self, make_track):
        short = make_track(duration=100)
        long_small = make_track(duration=200, filesize=1)
        long_big = make_track(duration=200, filesize=10**9)
        assert s.sort_tracks([long_small, short, long_big], "default") == [short, long_big, long_small]

    def test_default_sort_estimates_unknown_size(self, make_track):
        estimated = make_track(duration=200, filesize=0)          # est 200*20*1024 = 4_096_000
        smaller = make_track(duration=200, filesize=4_000_000)
        assert s.sort_tracks([smaller, estimated], "default") == [estimated, smaller]

    def test_filter_lossless(self, make_track):
        flac, k320, k256 = make_track(is_lossless=True), make_track(bitrate=320), make_track(bitrate=256)
        assert s.filter_lossless([flac, k320, k256]) == [flac, k320]


class TestFormatResults:
    def test_page_window_and_numbering(self, make_track):
        tracks = [make_track(artist=f"A{i}", title=f"T{i}") for i in range(12)]
        text = s._format_results(tracks, page=2, per_page=10, total=12)
        lines = text.splitlines()
        assert lines[0] == "🔍 Результаты 11-12 из 12"
        assert lines[2].startswith("11. A10 – T10")
        assert lines[3].startswith("12. A11 – T11")
        assert len([ln for ln in lines if ln and ln[0].isdigit()]) == 2

    def test_official_badge(self, make_track):
        text = s._format_results([make_track(artist="X - Topic", title="Y")], 1, 10, 1)
        assert "X - Topic – Y ✅" in text

    def test_cached_tracks_default_empty(self):
        assert s.get_cached_tracks(-12345) == []
