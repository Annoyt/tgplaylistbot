"""TrackInfo / UserSettings display helpers."""

import pytest

from app.db.models import UserSettings


class TestDurationStr:
    @pytest.mark.parametrize("sec,expected", [(0, "0:00"), (5, "0:05"), (65, "1:05"), (600, "10:00"), (3599, "59:59")])
    def test_formats_minutes_and_zero_padded_seconds(self, make_track, sec, expected):
        assert make_track(duration=sec).duration_str == expected


class TestSizeStr:
    def test_real_size_in_megabytes(self, make_track):
        assert make_track(filesize=5 * 1024 * 1024).size_str == "5.0M"

    def test_unknown_duration_shows_dash(self, make_track):
        assert make_track(duration=0, filesize=0).size_str == "—"

    def test_estimate_uses_known_bitrate(self, make_track):
        # 256s * 256kbps / 8 / 1024 = 8.0 MB
        assert make_track(duration=256, bitrate=256).size_str == "~8.0M"

    def test_estimate_assumes_320k_for_vk(self, make_track):
        # 256s * 320 / 8192 = 10.0
        assert make_track(duration=256, source="vk").size_str == "~10.0M"

    def test_estimate_assumes_128k_elsewhere(self, make_track):
        # 256s * 128 / 8192 = 4.0
        assert make_track(duration=256, source="youtube").size_str == "~4.0M"


class TestBitrateStr:
    def test_lossless_wins_over_bitrate(self, make_track):
        assert make_track(is_lossless=True, bitrate=320).bitrate_str == "FLAC"

    def test_known_bitrate(self, make_track):
        assert make_track(bitrate=320).bitrate_str == "320k"

    def test_estimated_from_size(self, make_track):
        # 2_000_000 B * 8 / 100 s / 1000 = 160 kbps
        assert make_track(duration=100, filesize=2_000_000).bitrate_str == "~160k"

    def test_estimate_below_sanity_threshold_hidden(self, make_track):
        # 20 kbps is not > 20 → hidden
        assert make_track(duration=100, filesize=250_000).bitrate_str == ""

    def test_estimate_just_above_threshold_shown(self, make_track):
        assert make_track(duration=100, filesize=262_500).bitrate_str == "~21k"

    def test_unknown_everything_is_empty(self, make_track):
        assert make_track(duration=0, filesize=0).bitrate_str == ""


class TestSource:
    @pytest.mark.parametrize("source,icon,name", [
        ("youtube", "▶️YT", "YouTube"),
        ("vk", "🎵VK", "VK"),
        ("spotify", "🟢SP", "Spotify"),
        ("deezer", "deezer", "Deezer"),
    ])
    def test_icon_and_name(self, make_track, source, icon, name):
        t = make_track(source=source)
        assert t.source_icon == icon
        assert t.source_name == name


def test_platforms_are_split_and_stripped():
    s = UserSettings(user_id=1, platform_priority="vk, youtube ,spotify")
    assert s.platforms == ["vk", "youtube", "spotify"]
