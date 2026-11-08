"""bot/handlers/search.py: sessions, result formatting, sorting/filtering, the search handler."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import search as s
from services import music_search as ms


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
        assert "✅" not in s._format_results([make_track(artist="7clouds", title="Song")], 1, 10, 1)

    def test_plus_when_more_can_be_fetched(self, make_track):
        tracks = [make_track()] * 50
        assert s._format_results(tracks, 1, 10, 50, more=True).splitlines()[0] == "🔍 Результаты 1-10 из 50+"

    def test_cached_tracks_default_empty(self):
        assert s.get_cached_tracks(-12345) == []

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

class TestClampPage:
    @pytest.mark.parametrize("page, total, expected", [
        (1, 0, 1), (5, 0, 1), (0, 30, 1), (-3, 30, 1),
        (3, 30, 3), (4, 30, 3), (20, 15, 2), (2, 11, 2), (2, 10, 1), (26, 260, 26), (27, 260, 26),
    ])
    def test_clamp(self, page, total, expected):
        assert s.clamp_page(page, total) == expected

    def test_custom_per_page(self):
        assert s.clamp_page(9, 13, per_page=6) == 3

    def test_empty_view_message(self):
        assert s._format_results([], 1, 10, 0) == "😔 Под этот фильтр ничего не подходит"

class TestFormatLineExact:
    def test_unofficial_line_has_no_badge_text(self, make_track):
        t = make_track(artist="7clouds", title="Song")
        line = s._format_results([t], 1, 10, 1).splitlines()[2]
        assert line == f"1. 7clouds – Song {t.duration_str} {t.source_icon} {t.size_str} {t.bitrate_str}"


class TestSessions:
    def test_store_and_get(self, make_track):
        t = make_track()
        try:
            session = s.store_results(777, [t], query="q")
            assert s.get_session(777) is session and s.get_cached_tracks(777) == [t]
            assert session.query == "q"
        finally:
            s._search_cache.pop(777, None)

    def test_without_query_there_is_nothing_more(self, make_track):
        try:
            assert not s.store_results(777, [make_track()]).has_more
        finally:
            s._search_cache.pop(777, None)

    def test_unknown_user(self):
        assert s.get_session(-1) is None and s.get_cached_tracks(-1) == []

    def test_oldest_session_evicted(self, make_track, monkeypatch):
        monkeypatch.setattr(s, "_MAX_CACHE", 2)
        try:
            for uid in (1, 2, 1, 3):          # 1 is re-used, so 2 is the oldest
                s.store_results(uid, [make_track()])
            assert set(s._search_cache) == {1, 3}
        finally:
            for uid in (1, 2, 3):
                s._search_cache.pop(uid, None)


class TestSplitQuery:
    @pytest.mark.parametrize("q,expected", [
        ("Кино - Группа крови", ("Кино", "Группа крови")),
        ("Кино — Группа крови", ("Кино", "Группа крови")),
        ("Кино -Группа крови", ("Кино", "Группа крови")),
        ("Кино Группа крови", ("", "Кино Группа крови")),
        ("- Группа крови", ("", "- Группа крови")),
        ("Jay-Z", ("", "Jay-Z")),
    ])
    def test_split(self, q, expected):
        assert s.split_query(q) == expected


def sources(monkeypatch, yt=(), vk=(), sp=()):
    """Patch every source; returns the mocks."""
    mocks = {
        "youtube": AsyncMock(return_value=list(yt)),
        "vk": AsyncMock(return_value=list(vk)),
        "spotify": AsyncMock(return_value=list(sp)),
    }
    monkeypatch.setattr(ms.yt_svc, "search", mocks["youtube"])
    monkeypatch.setattr(ms.vk_svc, "search", mocks["vk"])
    monkeypatch.setattr(ms.sp_svc, "search", mocks["spotify"])
    return mocks


def make_message(text, user_id=4242):
    status = SimpleNamespace(edit_text=AsyncMock())
    msg = SimpleNamespace(
        text=text, message_thread_id=None, delete=AsyncMock(),
        chat=SimpleNamespace(id=1, type="private", is_forum=False),
        bot=SimpleNamespace(send_message=AsyncMock(return_value=status)),
        from_user=SimpleNamespace(id=user_id),
    )
    return msg, status


class TestTextSearch:
    """First batch only: ~5 pages, the rest behind "Ещё"."""

    async def test_first_batch_sizes_and_more_button(self, db, monkeypatch, make_track):
        m = sources(monkeypatch,
                    yt=[make_track(title=f"Y{i}", source_id=f"y{i}") for i in range(20)],
                    vk=[make_track(title=f"V{i}", source="vk", source_id=f"v{i}") for i in range(30)])
        msg, status = make_message("Сломанный воздух")
        try:
            await s.text_search(msg)
            assert m["youtube"].await_args.kwargs == {"count": 20, "offset": 0}
            assert m["vk"].await_args.kwargs["count"] == 30 and m["vk"].await_args.kwargs["offset"] == 0
            assert m["spotify"].await_args.kwargs == {"count": 10, "offset": 0}
            text = status.edit_text.await_args.args[0]
            assert text.startswith("🔍 Результаты 1-10 из 50+")
            kb = status.edit_text.await_args.kwargs["reply_markup"]
            assert any(b.callback_data == "more::0" for row in kb.inline_keyboard for b in row)
            session = s.get_session(4242)
            assert len(session.tracks) == 50 and session.query == "Сломанный воздух"
            assert session.cursor.offsets == {"youtube": 20, "vk": 30, "spotify": 10}
            assert session.cursor.exhausted == {"spotify"}       # returned nothing
        finally:
            s._search_cache.pop(4242, None)

    async def test_nothing_found(self, db, monkeypatch):
        sources(monkeypatch)
        msg, status = make_message("zzzz")
        try:
            await s.text_search(msg)
            status.edit_text.assert_awaited_once_with("😔 Ничего не найдено")
        finally:
            s._search_cache.pop(4242, None)

    async def test_user_priority_orders_sources(self, db, monkeypatch, make_track):
        from app.db.database import get_db_ctx
        async with get_db_ctx() as conn:
            await conn.execute("INSERT INTO users (id) VALUES (4242)")
            await conn.execute("INSERT INTO user_settings (user_id, platform_priority) VALUES (4242, 'vk,youtube')")
            await conn.commit()
        sources(monkeypatch, yt=[make_track(source_id="y")], vk=[make_track(source="vk", source_id="v")])
        msg, _ = make_message("Song")
        try:
            await s.text_search(msg)
            assert s.get_session(4242).cursor.sources == ("vk", "youtube", "spotify")
        finally:
            s._search_cache.pop(4242, None)

    async def test_explicit_artist_title(self, db, monkeypatch, make_track):
        sources(monkeypatch, yt=[make_track(artist="Кино", title="Группа крови")])
        msg, _ = make_message("Кино - Группа крови")
        try:
            await s.text_search(msg)
            q = s.get_session(4242).rank_query
            assert (q.artist, q.title) == ("Кино", "Группа крови")
        finally:
            s._search_cache.pop(4242, None)

    async def test_short_query_ignored(self, db):
        msg, _ = make_message("a")
        await s.text_search(msg)
        msg.bot.send_message.assert_not_called()

    async def test_failure_is_reported(self, db, monkeypatch):
        monkeypatch.setattr(s, "new_search", AsyncMock(side_effect=RuntimeError("boom")))
        msg, status = make_message("Song")
        await s.text_search(msg)
        assert status.edit_text.await_args.args[0].startswith("❌")


class TestNewSearch:
    async def test_min_score_falls_back_when_nothing_passes(self, monkeypatch, make_track):
        sources(monkeypatch, yt=[make_track(artist="Metallica", title="One")])
        try:
            session = await s.new_search(9, "q", artist="Imagine Dragons", title="Believer", min_score=0.99)
            assert len(session.tracks) == 1 and session.min_score == 0.0
        finally:
            s._search_cache.pop(9, None)

    async def test_min_score_kept_when_something_passes(self, monkeypatch, make_track):
        sources(monkeypatch, yt=[make_track(artist="Imagine Dragons", title="Believer", source_id="a"),
                                 make_track(artist="Metallica", title="One", source_id="b")])
        try:
            session = await s.new_search(9, "q", artist="Imagine Dragons", title="Believer", min_score=0.6)
            assert [t.title for t in session.tracks] == ["Believer"] and session.min_score == 0.6
        finally:
            s._search_cache.pop(9, None)

    async def test_artist_mode(self, monkeypatch, make_track):
        sources(monkeypatch, vk=[make_track(artist="Кино", title="Кукушка", source="vk")])
        try:
            session = await s.new_search(9, "Кино", artist="Кино", title="", artist_mode=True)
            assert session.rank_query.artist_mode
        finally:
            s._search_cache.pop(9, None)
