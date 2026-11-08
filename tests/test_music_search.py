"""services/music_search.py: batches per source, offsets, "Ещё" (more)."""

from unittest.mock import AsyncMock

from services import music_search as ms
from services.ranking import Query


def patch_sources(monkeypatch, **behaviour):
    """behaviour[source] = list (returned), Exception (raised) or callable(offset)."""
    mocks = {}
    for source, attr, module in (("youtube", "search", ms.yt_svc), ("vk", "search", ms.vk_svc),
                                 ("spotify", "search", ms.sp_svc)):
        b = behaviour.get(source, [])
        if callable(b) and not isinstance(b, type):
            mock = AsyncMock(side_effect=lambda *a, _b=b, **kw: _b(kw["offset"]))
        elif isinstance(b, Exception):
            mock = AsyncMock(side_effect=b)
        else:
            mock = AsyncMock(return_value=list(b))
        monkeypatch.setattr(module, attr, mock)
        mocks[source] = mock
    return mocks


class TestCursor:
    def test_has_more_until_all_sources_exhausted(self):
        c = ms.Cursor()
        assert c.has_more
        c.exhausted |= {"youtube", "vk"}
        assert c.has_more
        c.exhausted.add("spotify")
        assert not c.has_more

    def test_only_listed_sources_count(self):
        assert not ms.Cursor(sources=("vk",), exhausted={"vk"}).has_more


class TestFetchBatch:
    async def test_asks_each_source_for_its_batch_at_its_offset(self, monkeypatch, make_track):
        m = patch_sources(monkeypatch, youtube=[make_track(source_id="y")], vk=[make_track(source="vk", source_id="v")])
        cursor = ms.Cursor(offsets={"youtube": 20, "vk": 30, "spotify": 10})
        out = await ms.fetch_batch("q", cursor, captcha_handler="H")
        assert m["youtube"].await_args.args == ("q",) and m["youtube"].await_args.kwargs == {"count": 20, "offset": 20}
        assert m["vk"].await_args.args == ("q",)
        assert m["vk"].await_args.kwargs == {"count": 30, "captcha_handler": "H", "offset": 30}
        assert m["spotify"].await_args.args == ("q",)
        assert m["spotify"].await_args.kwargs == {"count": 10, "offset": 10}
        assert [t.source_id for t in out] == ["y", "v"]
        assert cursor.offsets == {"youtube": 40, "vk": 60, "spotify": 20}
        assert cursor.exhausted == {"spotify"}

    async def test_order_follows_cursor_sources(self, monkeypatch, make_track):
        patch_sources(monkeypatch, youtube=[make_track(source_id="y")], vk=[make_track(source="vk", source_id="v")])
        out = await ms.fetch_batch("q", ms.Cursor(sources=("vk", "youtube", "spotify")))
        assert [t.source_id for t in out] == ["v", "y"]

    async def test_exhausted_sources_are_skipped(self, monkeypatch):
        m = patch_sources(monkeypatch)
        await ms.fetch_batch("q", ms.Cursor(exhausted={"youtube", "spotify"}))
        m["youtube"].assert_not_called()
        m["spotify"].assert_not_called()
        m["vk"].assert_awaited_once()

    async def test_failed_source_keeps_offset_and_stays_active(self, monkeypatch, make_track, caplog):
        patch_sources(monkeypatch, youtube=TimeoutError("slow"), vk=[make_track(source="vk")])
        cursor = ms.Cursor()
        with caplog.at_level("WARNING", logger="services.music_search"):
            out = await ms.fetch_batch("q", cursor)
        assert caplog.messages == ["youtube search failed: slow"]
        assert len(out) == 1
        assert "youtube" not in cursor.offsets and "youtube" not in cursor.exhausted

    async def test_unknown_source_names_ignored(self, monkeypatch):
        m = patch_sources(monkeypatch)
        await ms.fetch_batch("q", ms.Cursor(sources=("deezer", "vk")))
        m["vk"].assert_awaited_once()
        m["youtube"].assert_not_called()


class TestLoadMore:
    def session(self, tracks, **kw):
        return ms.SearchSession(query="Кино Группа крови", rank_query=Query("Кино", "Группа крови"),
                                tracks=list(tracks), cursor=ms.Cursor(), **kw)

    async def test_appends_ranked_new_tracks_after_the_seen_ones(self, monkeypatch, make_track):
        seen = make_track(artist="Кино", title="Группа крови", source="vk", source_id="v0")
        junk = make_track(artist="Кино", title="Группа крови (Караоке)", source="youtube", source_id="y1")
        orig = make_track(artist="Кино", title="Группа крови", source="youtube", source_id="y2")
        m = patch_sources(monkeypatch, youtube=[junk, orig], vk=[seen])       # VK repeats a shown track
        s = self.session([seen])
        assert await ms.load_more(s, captcha_handler="H") == 2
        assert m["vk"].await_args.args == ("Кино Группа крови",) and m["vk"].await_args.kwargs["captcha_handler"] == "H"
        assert s.tracks == [seen, orig, junk]

    async def test_duplicates_inside_a_batch_dropped(self, monkeypatch, make_track):
        a = make_track(source="vk", source_id="same")
        patch_sources(monkeypatch, vk=[a, make_track(source="vk", source_id="same")])
        s = self.session([])
        assert await ms.load_more(s) == 1

    async def test_min_score_applies_to_later_batches(self, monkeypatch, make_track):
        patch_sources(monkeypatch, youtube=[make_track(artist="Metallica", title="One")])
        s = self.session([], min_score=0.6)
        assert await ms.load_more(s) == 0 and s.tracks == []

    async def test_nothing_new_exhausts_sources(self, monkeypatch):
        patch_sources(monkeypatch)
        s = self.session([])
        assert await ms.load_more(s) == 0
        assert not s.has_more

    def test_session_without_query_has_no_more(self):
        assert not ms.SearchSession(query="", rank_query=Query(), tracks=[]).has_more
        assert not ms.SearchSession(query="q", rank_query=Query(), tracks=[]).has_more  # default cursor: all done
