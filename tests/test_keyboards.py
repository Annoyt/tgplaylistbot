"""Inline keyboards: labels, callback payloads, Telegram's 64-byte callback_data limit."""

import pytest

from bot.keyboards import inline as kb

TELEGRAM_CALLBACK_LIMIT = 64  # bytes, not characters


def all_callback_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def assert_within_limit(markup):
    for data in all_callback_data(markup):
        assert len(data.encode("utf-8")) <= TELEGRAM_CALLBACK_LIMIT, data


class TestMbLabel:
    @pytest.mark.parametrize("size,expected", [
        (0, "720p"),
        (-5, "720p"),
        (None, "720p"),
        (1, "720p (~1KB)"),               # rounds up to at least 1KB
        (220 * 1024, "720p (~220KB)"),
        (1024 * 1024 - 1, "720p (~1024KB)"),
        (1024 * 1024, "720p (~1.0MB)"),
        (int(9.94 * 1024 * 1024), "720p (~9.9MB)"),
        (10 * 1024 * 1024, "720p (~10MB)"),
        (232 * 1024 * 1024, "720p (~232MB)"),
    ])
    def test_formats(self, size, expected):
        assert kb._mb_label("720p", size) == expected


class TestLinkMenu:
    def test_buttons_and_payloads(self):
        m = kb.link_menu_kb("tok123")
        assert all_callback_data(m) == ["lv:tok123", "lva:tok123", "lr:tok123"]
        assert [b.text for row in m.inline_keyboard for b in row] == [
            "🎬 Скачать видео", "🎧 Звук MP3", "🎵 Найти трек из видео",
        ]


class TestVideoRes:
    def test_payloads_and_size_labels(self):
        m = kb.video_res_kb("t", {"best": 20 * 1024 * 1024, "720": 5 * 1024 * 1024, "360": 0})
        assert all_callback_data(m) == ["lvr:t:best", "lvr:t:720", "lvr:t:360", "lva:t", "lvf:t"]
        texts = [b.text for b in m.inline_keyboard[0]]
        assert texts == ["🏆 Лучшее (~20MB)", "720p (~5.0MB)", "360p"]

    def test_no_sizes(self):
        texts = [b.text for b in kb.video_res_kb("t").inline_keyboard[0]]
        assert texts == ["🏆 Лучшее", "720p", "360p"]


class TestSearchResults:
    def test_select_buttons_rows_of_five(self, make_track):
        m = kb.search_results_kb([make_track()] * 30, page=2, total=30)
        assert all_callback_data(m)[:10] == [f"select_track:{i}:2" for i in range(10, 20)]
        assert len(m.inline_keyboard[0]) == 5 and len(m.inline_keyboard[1]) == 5

    def test_last_partial_page(self, make_track):
        m = kb.search_results_kb([make_track()] * 12, page=2, total=12)
        selects = [d for d in all_callback_data(m) if d.startswith("select_track")]
        assert selects == ["select_track:10:2", "select_track:11:2"]

    def test_pagination_marks_current_page_and_prev(self, make_track):
        m = kb.search_results_kb([make_track()] * 30, page=2, total=30, sort_by="bitrate_desc")
        nav = [b for b in m.inline_keyboard[2]]
        assert nav[0].text == "◀️" and nav[0].callback_data == "page:1:bitrate_desc:0"
        assert [b.text for b in nav[1:]] == ["1", "·2·", "3"]

    def test_second_nav_row_and_next_arrow_for_many_pages(self, make_track):
        m = kb.search_results_kb([make_track()] * 100, page=1, total=100)
        rows = m.inline_keyboard
        assert rows[2][-1].text == "▶️" and rows[2][-1].callback_data == "page:2::0"
        assert [b.text for b in rows[3]] == ["6", "7", "8", "9", "10"]

    def test_filter_toggles(self, make_track):
        m = kb.search_results_kb([make_track()], page=1, total=1, sort_by="bitrate_desc", lossless_only=True)
        flt = m.inline_keyboard[-1]
        assert flt[0].text == "BR: ↓" and flt[0].callback_data == "filter:br:bitrate_asc:1"
        assert flt[1].text == "Lossless: ✅" and flt[1].callback_data == "filter:lossless:0:1"

    def test_popular_artist_button(self, make_track):
        m = kb.search_results_kb([make_track()], page=1, total=1, artist_name="Imagine Dragons")
        assert m.inline_keyboard[-1][0].callback_data == "popular_artist:Imagine Dragons"

    @pytest.mark.parametrize("artist", [
        "A" * 80,
        "Очень Длинное Имя Исполнителя Из Кириллицы",   # 2 bytes per letter
        "🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸🎸",               # 4 bytes per emoji
    ])
    def test_callback_data_never_exceeds_64_bytes(self, make_track, artist):
        m = kb.search_results_kb([make_track()] * 120, page=12, total=120, sort_by="bitrate_desc",
                                 lossless_only=True, artist_name=artist)
        assert_within_limit(m)

    def test_truncated_artist_is_valid_utf8_prefix(self, make_track):
        artist = "Очень Длинное Имя Исполнителя Из Кириллицы"
        m = kb.search_results_kb([make_track()], page=1, total=1, artist_name=artist)
        data = m.inline_keyboard[-1][0].callback_data
        name = data.removeprefix("popular_artist:").removesuffix("..")
        assert artist.startswith(name) and len(name) > 0


class TestOtherKeyboards:
    def test_track_detail_cached(self):
        m = kb.track_detail_kb(7, is_cached=True, page=3)
        assert all_callback_data(m) == ["dl:7:mp3_320", "dl:7:mp3_320:1", "back_to_list:3"]

    def test_track_detail_uncached_names_source(self):
        m = kb.track_detail_kb(7, source_name="VK")
        assert m.inline_keyboard[0][0].text == "📥 Скачать из VK"
        assert all_callback_data(m) == ["dl:7:mp3_320", "back_to_list:1"]

    def test_settings_marks_quality(self):
        m = kb.settings_kb("flac", "vk,youtube")
        row = m.inline_keyboard[1]
        assert row[0].text == "MP3 320kbps" and row[1].text == "✅ FLAC"
        assert m.inline_keyboard[3][0].text == "📋 vk,youtube"

    def test_confirm_topic(self):
        assert all_callback_data(kb.confirm_topic_kb("🔥", 4)) == ["topic:create:🔥:4", "topic:cancel"]

    def test_audiobook_results_pagination(self):
        m = kb.audiobook_results_kb(total=14, page=2, per_page=6)
        assert all_callback_data(m) == [f"bsel:{i}:2" for i in range(6, 12)] + ["bpage:1", "bpage:noop", "bpage:3"]
        assert m.inline_keyboard[-1][1].text == "2/3"

    def test_audiobook_single_page_has_no_arrows(self):
        assert all_callback_data(kb.audiobook_results_kb(total=2, page=1)) == ["bsel:0:1", "bsel:1:1", "bpage:noop"]

    def test_audiobook_detail(self):
        m = kb.audiobook_detail_kb(3, "LibriVox", page=2)
        assert m.inline_keyboard[0][0].text == "📥 Скачать книгу из LibriVox"
        assert all_callback_data(m) == ["bdl:3", "bback:2"]
        assert kb.audiobook_detail_kb(3).inline_keyboard[0][0].text == "📥 Скачать книгу"

    def test_main_reply_kb(self):
        m = kb.main_reply_kb()
        assert [b.text for b in m.keyboard[0]] == [kb.BTN_MUSIC, kb.BTN_AUDIOBOOKS]


class TestFitCallback:
    def test_short_value_untouched(self):
        assert kb._fit_callback("p:", "abc") == "p:abc"

    def test_exactly_at_limit_untouched(self):
        value = "a" * (64 - 2)
        assert kb._fit_callback("p:", value) == "p:" + value

    def test_one_byte_over_is_cut_and_marked(self):
        out = kb._fit_callback("p:", "a" * 63)
        assert out == "p:" + "a" * 60 + ".." and len(out.encode()) == 64

    def test_never_splits_a_multibyte_character(self):
        out = kb._fit_callback("p:", "Ж" * 40)          # 80 bytes of 2-byte letters
        assert out.endswith("..") and len(out.encode()) <= 64
        assert set(out[2:-2]) == {"Ж"}
