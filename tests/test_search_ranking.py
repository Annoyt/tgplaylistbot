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

    def test_soft_junk_tag_in_uploader_ignored(self, make_track):
        assert s._junk_penalty(make_track(artist="Best Greatest Hits", title="Song", duration=200), "x", False) == 0.0

    def test_hard_junk_tag_in_uploader_counts(self, make_track):
        assert s._junk_penalty(make_track(artist="Lex Fridman Podcast", title="Talk #1", duration=200), "x", False) == 2.0

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

    @pytest.mark.parametrize("title", ["Believer (Slowed + Reverb)", "Believer [Live]", "Believer (Remix)"])
    def test_bracketed_edits_get_full_penalty(self, make_track, title):
        orig = make_track(artist="Imagine Dragons", title="Believer")
        edit = make_track(artist="Imagine Dragons", title=title)
        assert s.relevance_score(orig, "Imagine Dragons", "Believer") == 1.0
        assert s.relevance_score(edit, "Imagine Dragons", "Believer") == pytest.approx(0.75)

    def test_unbracketed_edit_penalised(self, make_track):
        orig = make_track(artist="Imagine Dragons", title="Believer")
        edit = make_track(artist="Imagine Dragons", title="Believer slowed")
        assert s.relevance_score(edit, "Imagine Dragons", "Believer") <= \
            s.relevance_score(orig, "Imagine Dragons", "Believer") - 0.25

    def test_edit_not_penalised_when_asked_for(self, make_track):
        orig = make_track(artist="Imagine Dragons", title="Believer")
        edit = make_track(artist="Imagine Dragons", title="Believer (Slowed + Reverb)")
        # same normalised text, so equal scores mean the edit got no penalty
        assert s.relevance_score(edit, "Imagine Dragons", "Believer slowed") == \
            s.relevance_score(orig, "Imagine Dragons", "Believer slowed")

    def test_different_edit_than_asked_is_penalised(self, make_track):
        live = make_track(artist="Imagine Dragons", title="Believer (Live)")
        orig = make_track(artist="Imagine Dragons", title="Believer")
        assert s.relevance_score(live, "Imagine Dragons", "Believer remix") < \
            s.relevance_score(orig, "Imagine Dragons", "Believer remix")

    def test_edit_tag_inside_a_word_is_not_an_edit(self, make_track):
        # "live" inside "Oliver", "cover" inside "Discovery"
        t = make_track(artist="Oliver Tree", title="Discovery")
        assert s.relevance_score(t, "Oliver Tree", "Discovery") == 1.0

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


class TestWholeWordTags:
    def test_news_band_is_not_junk(self, make_track):
        t = make_track(artist="Huey Lewis & The News", title="The Power of Love", duration=230)
        assert s._junk_penalty(t, "power of love", False) == 0.0

    def test_vypusknoy_is_not_an_episode(self, make_track):
        t = make_track(artist="Баста", title="Выпускной", duration=230)
        assert s._junk_penalty(t, "баста выпускной", False) == 0.0

    def test_real_episode_still_hard_junk(self, make_track):
        t = make_track(artist="Радио", title="Выпуск 12", duration=230)
        assert s._junk_penalty(t, "x", False) == 2.0

    @pytest.mark.parametrize("q", ["Сейчас", "Imagine Dragons remix"])
    def test_words_containing_tags_are_not_longform(self, q):
        assert not s._is_longform_query(q)

    def test_unofficial_is_not_official(self, make_track):
        assert not s._is_official(make_track(artist="Unofficial Uploads"))
        assert s._official_boost(make_track(artist="x", title="Song (Unofficial Video)")) == 0.0

    def test_bracketed_junk_tag_counts(self, make_track):
        t = make_track(title="Song (Full Album)", duration=200)
        assert s._junk_penalty(t, "song", False) == 1.0


class TestEditsRankBelowOriginal:
    def test_official_original_beats_320k_slowed_and_live_copies(self, make_track):
        official = make_track(artist="Imagine Dragons - Topic", title="Believer", source="youtube", duration=204)
        slowed = make_track(artist="Imagine Dragons", title="Believer (Slowed + Reverb)", source="vk", bitrate=320, duration=250)
        live = make_track(artist="Imagine Dragons", title="Believer (Live)", source="vk", bitrate=320, duration=215)
        out = s.rank_by_relevance([slowed, live, official], "Imagine Dragons", "Believer")
        assert out[0] is official

    def test_edit_not_grouped_with_original(self, make_track):
        orig = make_track(artist="Imagine Dragons", title="Believer", duration=204)
        other = make_track(artist="Metallica", title="One", duration=446)
        slowed = make_track(artist="Imagine Dragons", title="Believer (Slowed)", source="vk", bitrate=320, duration=230)
        # would merge on tokens + duration; the edit tag keeps them apart
        assert s._group_same_track([orig, other, slowed]) == [orig, other, slowed]

    def test_same_edit_still_groups(self, make_track):
        a = make_track(artist="Imagine Dragons", title="Believer (Live)", duration=215)
        other = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="Imagine Dragons", title="Believer [Live]", source="vk", bitrate=320, duration=216)
        assert s._group_same_track([a, other, b]) == [b, a, other]


# ── written to kill surviving mutants (mutmut) ─────────────────────────────

from difflib import SequenceMatcher  # noqa: E402


class TestRelevanceComponents:
    def test_title_only_similarity_counts(self, make_track):
        # uploader name is noise; only the title-vs-title ratio reaches 1.0
        t = make_track(artist="Some Random Uploader", title="Believer")
        assert s.relevance_score(t, "", "Believer") == 1.0

    def test_title_containment_bonus_is_added(self, make_track):
        t = make_track(artist="x", title="Believer extended radio edit version")
        q, tt = "believer", "believer extended radio edit version"
        base = max(SequenceMatcher(None, q, tt).ratio(), SequenceMatcher(None, q, "x " + tt).ratio(),
                   s._token_set_ratio(q, "x " + tt))
        assert s.relevance_score(t, "", "Believer") == pytest.approx(base + 0.12)

    def test_artist_containment_bonus_is_added(self, make_track):
        t = make_track(artist="Imagine Dragons", title="Completely Different Words Here")
        q_c, t_c = "imagine dragons zzz", "imagine dragons completely different words here"
        base = max(SequenceMatcher(None, "zzz", "completely different words here").ratio(),
                   SequenceMatcher(None, q_c, t_c).ratio(), s._token_set_ratio(q_c, t_c))
        assert s.relevance_score(t, "Imagine Dragons", "zzz") == pytest.approx(base + 0.12)

    def test_no_bonus_without_containment(self, make_track):
        t = make_track(artist="aa", title="bb")
        base = max(SequenceMatcher(None, "dd", "bb").ratio(), SequenceMatcher(None, "cc dd", "aa bb").ratio(),
                   s._token_set_ratio("cc dd", "aa bb"))
        assert s.relevance_score(t, "cc", "dd") == pytest.approx(base)


class TestQualityBoundaries:
    def test_tiny_known_bitrate_is_used(self, make_track):
        assert s.quality_score(make_track(bitrate=1, source="youtube")) == pytest.approx(1 / 320)

    def test_known_bitrate_beats_size_estimate(self, make_track):
        t = make_track(bitrate=160, duration=200, filesize=8_000_000)   # size alone would say 320k
        assert s.quality_score(t) == pytest.approx(0.5)

    def test_one_byte_file_is_derived_not_defaulted(self, make_track):
        assert s.quality_score(make_track(duration=100, filesize=1)) == pytest.approx(8e-5 / 320, abs=1e-9)

    def test_zero_duration_with_size_uses_default(self, make_track):
        assert s.quality_score(make_track(duration=0, filesize=5000, source="youtube")) == pytest.approx(0.6)

    def test_one_second_duration_is_derived(self, make_track):
        assert s.quality_score(make_track(duration=1, filesize=40_000)) == 1.0     # 320 kbps


class TestGroupingBoundaries:
    def test_shared_artist_different_songs_same_length_stay_apart(self, make_track):
        a = make_track(artist="Imagine Dragons", title="Believer", duration=200)
        x = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="Imagine Dragons", title="Thunder", duration=200)
        assert s._group_same_track([a, x, b]) == [a, x, b]

    def test_second_group_can_match_after_first_mismatch(self, make_track):
        g1 = make_track(artist="Metallica", title="One", duration=446)
        g2 = make_track(artist="Imagine Dragons", title="Believer", duration=204)
        dup = make_track(artist="Imagine Dragons", title="Believer", source="vk", bitrate=320, duration=205)
        assert s._group_same_track([g1, g2, dup]) == [g1, dup, g2]

    def test_tokenless_track_never_merges(self, make_track):
        empty = make_track(artist="", title="the official video", duration=200)
        x = make_track(artist="Metallica", title="One", duration=446)
        real = make_track(artist="A", title="Song", duration=200)
        assert s._group_same_track([empty, x, real]) == [empty, x, real]
        assert s._group_same_track([real, x, empty]) == [real, x, empty]

    @pytest.mark.parametrize("d1,d2", [(0, 200), (200, 0)])
    def test_unknown_duration_still_merges(self, make_track, d1, d2):
        a = make_track(artist="A", title="Song", duration=d1)
        x = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="A", title="Song", source="vk", bitrate=320, duration=d2)
        assert s._group_same_track([a, x, b]) == [b, a, x]

    def test_exactly_30_percent_longer_merges(self, make_track):
        a = make_track(artist="A", title="Song", duration=200)
        x = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="A", title="Song", source="vk", bitrate=320, duration=260)
        assert s._group_same_track([a, x, b]) == [b, a, x]

    def test_half_longer_does_not_merge(self, make_track):
        a = make_track(artist="A", title="Song", duration=200)
        x = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="A", title="Song", source="vk", bitrate=320, duration=300)
        assert s._group_same_track([a, x, b]) == [a, x, b]

    def test_exactly_70_percent_overlap_merges(self, make_track):
        a = make_track(artist="w1 w2 w3 w4 w5", title="w6 w7 w8 w9 w10", duration=200)
        x = make_track(artist="Metallica", title="One", duration=446)
        b = make_track(artist="w1 w2 w3 w4 w5", title="w6 w7 q8 q9 q10", source="vk", bitrate=320, duration=200)
        assert s._group_same_track([a, x, b]) == [b, a, x]

    def test_two_letter_words_are_tokens_one_letter_are_not(self, make_track):
        assert s._track_tokens(make_track(artist="AC DC", title="x")) == {"ac", "dc"}


class TestRankingBoundaries:
    def test_quality_decides_between_equally_relevant_songs(self, make_track):
        low = make_track(artist="B", title="Song", source="youtube", bitrate=128)
        high = make_track(artist="A", title="Song", source="vk", bitrate=320)
        assert s.rank_by_relevance([low, high], "", "Song")[0] is high

    def test_longform_query_does_not_sink_long_items(self, make_track):
        mix = make_track(artist="Chill", title="Lofi mix podcast", duration=7200)
        short = make_track(artist="Other", title="Lofi", duration=200, source="youtube", bitrate=1)
        out = s.rank_by_relevance([short, mix], "", "lofi mix podcast")
        assert out[0] is mix

    def test_score_equal_to_min_is_kept(self, make_track):
        t = make_track(artist="A", title="Song")
        rel = s.relevance_score(t, "A", "Song")
        assert s.rank_by_relevance([t], "A", "Song", min_score=rel) == [t]

    def test_reference_length_ignores_unknown_durations(self, make_track):
        unknown = make_track(artist="Z", title="Song", duration=0)
        long_ = make_track(artist="X", title="Song", duration=210)
        exact = make_track(artist="Y", title="Song", duration=200)
        # median of known lengths = 205 → both equidistant → original order (long_ first)
        out = s.rank_by_relevance([long_, exact, unknown], "", "Song")
        assert out.index(long_) < out.index(exact)


class TestSmallHelpers:
    def test_tag_pattern_whole_words_longest_first_and_escaped(self):
        p = s._tag_pattern(("sped up", "sped", "8d", "a+b"))
        assert s._tags_in("Song (Sped Up) 8D a+b", p) == {"sped up", "8d", "a+b"}
        assert s._tags_in("speedy 8dd", p) == set()

    def test_soft_tag_and_long_duration_add_up(self, make_track):
        assert s._junk_penalty(make_track(title="Greatest Hits", duration=1500), "x", False) == pytest.approx(1.6)
        assert s._junk_penalty(make_track(title="Episode 1", duration=700), "x", False) == pytest.approx(2.2)

    def test_duration_score_at_one(self):
        assert s._duration_score(1, 1.0) == 1.0
        assert s._duration_score(2, 1.0) == 0.0

    def test_topic_suffix_without_dash_is_official(self, make_track):
        assert s._is_official(make_track(artist="ImagineDragonsTopic"))

    def test_no_badge_for_unofficial(self, make_track):
        assert "✅" not in s._format_results([make_track(artist="7clouds", title="Song")], 1, 10, 1)


class TestSortBoundaries:
    def test_title_sort_is_lowercase_based(self, make_track):
        under, b = make_track(title="_x"), make_track(title="b")
        assert s.sort_tracks([b, under], "title") == [under, b]

    def test_estimate_constant_is_20_kib_per_second(self, make_track):
        just_bigger = make_track(duration=200, filesize=200 * 20 * 1024 + 1)
        estimated = make_track(duration=200, filesize=0)
        assert s.sort_tracks([estimated, just_bigger], "default") == [just_bigger, estimated]

    def test_one_byte_file_is_not_estimated(self, make_track):
        tiny = make_track(duration=200, filesize=1)
        estimated = make_track(duration=200, filesize=0)
        assert s.sort_tracks([tiny, estimated], "default") == [estimated, tiny]
