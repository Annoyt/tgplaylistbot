"""bot/handlers/callbacks.py: result paging/sorting, track selection and download."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.database import get_db_ctx
from bot.handlers import callbacks as cb
from bot.handlers import search


def make_callback(data, chat_type="private", user_id=42, chat_id=777):
    bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(username="plcm_bot")))
    status = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
    msg = SimpleNamespace(
        message_id=600, text="old results",
        chat=SimpleNamespace(id=chat_id, type=chat_type), bot=bot,
        edit_text=AsyncMock(), answer=AsyncMock(return_value=status),
        answer_audio=AsyncMock(return_value=SimpleNamespace(message_id=900, audio=SimpleNamespace(file_id="new-fid"))),
    )
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=user_id, username="u", first_name="U"),
                           answer=AsyncMock(), message=msg, bot=bot), status


@pytest.fixture
def cached(make_track):
    """User 42's cached results: unsorted, mixed bitrates, one lossless."""
    tracks = [
        make_track(title="Low", artist="A", bitrate=128, source_id="low"),
        make_track(title="High", artist="A", bitrate=320, source_id="high"),
        make_track(title="Flac", artist="A", is_lossless=True, source_id="flac"),
        make_track(title="Mid", artist="A", bitrate=192, source_id="mid"),
    ]
    search.store_results(42, tracks)
    yield tracks
    search._search_cache.pop(42, None)


def select_buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith("select_track:")]


class TestIndexMap:
    def test_maps_view_to_cache_positions(self, make_track):
        a, b, c = make_track(), make_track(), make_track()
        assert cb._index_map([a, b, c], [c, a]) == [2, 0]

    def test_identity_not_equality(self, make_track):
        a, b = make_track(), make_track()          # equal dataclasses, different objects
        assert cb._index_map([a, b], [b]) == [1]


class TestSortedViewsSelectTheShownTrack:
    async def test_bitrate_sort_buttons_point_into_cache(self, cached):
        callback, _ = make_callback("filter:br:bitrate_desc:1")
        await cb.cb_filter(callback)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        shown = sorted(cached, key=lambda t: t.bitrate, reverse=True)
        # button i must select the i-th *shown* track
        assert [cached[int(d.split(":")[1])] for d in select_buttons(markup)] == shown
        assert callback.message.edit_text.await_args.kwargs["parse_mode"] == "HTML"

    async def test_lossless_filter_buttons_point_into_cache(self, cached):
        callback, _ = make_callback("filter:lossless:1:1")
        await cb.cb_filter(callback)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert [cached[int(d.split(":")[1])].source_id for d in select_buttons(markup)] == ["high", "flac"]

    async def test_page_with_sort_and_filter(self, cached):
        callback, _ = make_callback("page:1:bitrate_asc:1")
        await cb.cb_page(callback)
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert [cached[int(d.split(":")[1])].source_id for d in select_buttons(markup)] == ["flac", "high"]

    async def test_then_select_opens_that_track(self, cached, db):
        callback, _ = make_callback("filter:br:bitrate_desc:1")
        await cb.cb_filter(callback)
        first = select_buttons(callback.message.edit_text.await_args.kwargs["reply_markup"])[0]
        sel, _ = make_callback(first)
        await cb.cb_select_track(sel)
        assert "High" in sel.message.edit_text.await_args.args[0]

    async def test_expired_results(self):
        search._search_cache.pop(42, None)
        for handler, data in ((cb.cb_page, "page:1::0"), (cb.cb_filter, "filter:br:bitrate_desc:1")):
            callback, _ = make_callback(data)
            await handler(callback)
            callback.answer.assert_awaited_once()
            callback.message.edit_text.assert_not_called()


class TestSelectTrack:
    async def test_detail_is_html_escaped(self, db, make_track):
        search.store_results(42, [make_track(artist="A&B", title="I <3 you")])
        callback, _ = make_callback("select_track:0:1")
        await cb.cb_select_track(callback)
        text = callback.message.edit_text.await_args.args[0]
        assert "A&amp;B – I &lt;3 you" in text
        search._search_cache.pop(42, None)

    async def test_out_of_range(self, cached):
        callback, _ = make_callback("select_track:99:1")
        await cb.cb_select_track(callback)
        callback.answer.assert_awaited_once_with("❌ Сессия поиска истекла.")


class TestDownload:
    @pytest.fixture
    def patched(self, monkeypatch, tmp_path):
        state = SimpleNamespace(saved=[], file=tmp_path / "t.mp3")
        state.file.write_bytes(b"x" * 1000)
        monkeypatch.setattr(cb, "get_cached_file_id", AsyncMock(return_value=None))

        async def save(*args):
            state.saved.append(args)
        monkeypatch.setattr(cb, "save_cached_file_id", save)
        monkeypatch.setattr(cb, "download_track", AsyncMock(return_value=str(state.file)))
        monkeypatch.setattr(cb, "get_audio_duration_ffprobe", AsyncMock(return_value=0))
        return state

    async def test_download_send_cache_and_cleanup(self, db, cached, patched):
        callback, status = make_callback("dl:1:mp3_320")
        await cb.cb_download(callback)
        kwargs = callback.message.answer_audio.await_args.kwargs
        assert kwargs["performer"] == "A" and kwargs["caption"].endswith("\n\n🤖 @plcm_bot")
        assert patched.saved == [("A", "High", 200, "new-fid")]
        status.delete.assert_awaited_once()
        assert not patched.file.exists()

    async def test_cached_file_id_is_sent_without_download(self, db, cached, patched, monkeypatch):
        monkeypatch.setattr(cb, "get_cached_file_id", AsyncMock(return_value="cached-fid"))
        callback, _ = make_callback("dl:0:mp3_320")
        await cb.cb_download(callback)
        assert callback.message.answer_audio.await_args.kwargs["audio"] == "cached-fid"
        cb.download_track.assert_not_called()

    async def test_stale_cached_id_falls_back_to_download(self, db, cached, patched, monkeypatch):
        monkeypatch.setattr(cb, "get_cached_file_id", AsyncMock(return_value="stale"))
        callback, _ = make_callback("dl:0:mp3_320")
        callback.message.answer_audio.side_effect = [RuntimeError("file not found"),
                                                     SimpleNamespace(message_id=1, audio=SimpleNamespace(file_id="fresh"))]
        await cb.cb_download(callback)
        assert patched.saved[-1][-1] == "fresh"

    async def test_user_quality_overrides_button(self, db, cached, patched):
        async with get_db_ctx() as conn:
            await conn.execute("INSERT INTO users (id) VALUES (42)")
            await conn.execute("INSERT INTO user_settings (user_id, default_quality) VALUES (42, 'flac')")
            await conn.commit()
        callback, _ = make_callback("dl:0:mp3_320")
        await cb.cb_download(callback)
        assert cb.download_track.await_args.args[1] == "flac"

    async def test_download_failure(self, db, cached, patched, monkeypatch):
        monkeypatch.setattr(cb, "download_track", AsyncMock(return_value=None))
        callback, status = make_callback("dl:0:mp3_320")
        await cb.cb_download(callback)
        status.edit_text.assert_awaited_once_with("❌ Не удалось скачать трек.")

    async def test_too_big_file_refused(self, db, cached, patched, monkeypatch):
        monkeypatch.setattr(cb.settings, "telegram_api_base_url", "")
        monkeypatch.setattr(cb.os.path, "getsize", lambda p: 60 * 1024 * 1024)
        callback, status = make_callback("dl:0:mp3_320")
        await cb.cb_download(callback)
        assert "слишком большой" in status.edit_text.await_args.args[0]
        callback.message.answer_audio.assert_not_called()

    async def test_long_title_answer_is_capped(self, db, patched, make_track):
        search.store_results(42, [make_track(artist="A" * 150, title="T" * 150)])
        callback, _ = make_callback("dl:0:mp3_320")
        await cb.cb_download(callback)
        assert len(callback.answer.await_args.args[0]) <= 200
        search._search_cache.pop(42, None)

    async def test_unknown_index(self, cached):
        callback, _ = make_callback("dl:9:mp3_320")
        await cb.cb_download(callback)
        callback.answer.assert_awaited_once_with("Трек не найден.")

    async def test_group_download_links_session_from_same_chat_only(self, db, cached, patched):
        async with get_db_ctx() as conn:
            await conn.execute("INSERT INTO search_sessions (session_id, user_id, chat_id, general_msg_ids, query,"
                               " original_msg_id, created_at) VALUES ('s1', 42, -100, '[]', 'q', 11, '2026-01-01')")
            await conn.execute("INSERT INTO search_sessions (session_id, user_id, chat_id, general_msg_ids, query,"
                               " original_msg_id, created_at) VALUES ('s2', 42, 42, '[]', 'q', 1234, '2026-02-01')")
            await conn.commit()
        callback, _ = make_callback("dl:1:mp3_320", chat_type="supergroup", chat_id=-100)
        await cb.cb_download(callback)
        async with get_db_ctx() as conn:
            rows = await (await conn.execute("SELECT chat_id, audio_msg_id, search_msg_id, original_msg_id FROM pending_tracks")).fetchall()
        # the newer DM session (1234) must not leak into the group
        assert [tuple(r) for r in rows] == [(-100, 900, 600, 11)]
        assert "🤖" not in callback.message.answer_audio.await_args.kwargs["caption"]


class TestLongResultLists:
    """Lists of 200+ results: page clamping and re-tapping the current page."""

    @pytest.fixture
    def many(self, make_track):
        tracks = [make_track(title=f"T{i}", artist="A", bitrate=320 if i < 15 else 128, source_id=f"s{i}")
                  for i in range(260)]
        search.store_results(42, tracks)
        yield tracks
        search._search_cache.pop(42, None)

    async def test_last_page_of_260(self, many):
        callback, _ = make_callback("page:26::0")
        await cb.cb_page(callback)
        text = callback.message.edit_text.await_args.args[0]
        assert text.startswith("🔍 Результаты 251-260 из 260")
        assert select_buttons(callback.message.edit_text.await_args.kwargs["reply_markup"])[-1] == "select_track:259:26"

    async def test_filter_clamps_page_to_what_is_left(self, many):
        # Lossless on at page 20: 15 tracks are left = 2 pages → show page 2,
        # not "191-15 из 15" with no tracks.
        callback, _ = make_callback("filter:lossless:1:20")
        await cb.cb_filter(callback)
        text = callback.message.edit_text.await_args.args[0]
        assert text.startswith("🔍 Результаты 11-15 из 15")
        assert select_buttons(callback.message.edit_text.await_args.kwargs["reply_markup"]) == [
            f"select_track:{i}:2" for i in range(10, 15)]

    async def test_back_to_list_clamps_page(self, many):
        callback, _ = make_callback("back_to_list:99")
        await cb.cb_back_to_list(callback)
        assert callback.message.edit_text.await_args.args[0].startswith("🔍 Результаты 251-260 из 260")
        callback.answer.assert_awaited_once()

    async def test_filter_with_no_matches(self, make_track):
        search.store_results(42, [make_track(bitrate=128)])
        callback, _ = make_callback("filter:lossless:1:1")
        await cb.cb_filter(callback)
        assert callback.message.edit_text.await_args.args[0] == "😔 Под этот фильтр ничего не подходит"
        search._search_cache.pop(42, None)

    async def test_tapping_the_shown_page_still_answers(self, many):
        from aiogram.exceptions import TelegramBadRequest
        callback, _ = make_callback("page:1::0")
        callback.message.edit_text.side_effect = TelegramBadRequest(
            method=None, message="Bad Request: message is not modified: specified new message content ...")
        await cb.cb_page(callback)
        callback.answer.assert_awaited_once_with()

    async def test_other_edit_errors_propagate(self, many):
        from aiogram.exceptions import TelegramBadRequest
        callback, _ = make_callback("page:1::0")
        callback.message.edit_text.side_effect = TelegramBadRequest(method=None, message="Bad Request: message to edit not found")
        with pytest.raises(TelegramBadRequest):
            await cb.cb_page(callback)


class TestMoreButton:
    """"Ещё": the next batch from the sources, appended after what was shown."""

    @pytest.fixture
    def session(self, make_track):
        from services.music_search import Cursor
        from services.ranking import Query
        tracks = [make_track(title=f"T{i}", artist="A", source="vk", source_id=f"s{i}", bitrate=128) for i in range(50)]
        sess = search.store_results(42, tracks, query="A", rank_query=Query("A", ""), cursor=Cursor())
        yield sess
        search._search_cache.pop(42, None)

    @pytest.fixture
    def more(self, monkeypatch, make_track):
        """Patch load_more: appends ``n`` tracks (set via the returned holder)."""
        holder = SimpleNamespace(n=12, calls=0, bitrate=320)

        async def fake(sess, captcha_handler=None):
            holder.calls += 1
            new = [make_track(title=f"N{i}", artist="A", source="vk", source_id=f"n{holder.calls}-{i}",
                              bitrate=holder.bitrate) for i in range(holder.n)]
            sess.tracks.extend(new)
            if holder.n == 0:
                sess.cursor.exhausted |= set(sess.cursor.sources)
            return holder.n
        monkeypatch.setattr(cb, "load_more", fake)
        return holder

    async def test_appends_and_shows_the_first_new_page(self, session, more):
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        callback.answer.assert_awaited_once_with("🔎 Ищу ещё…")
        text = callback.message.edit_text.await_args.args[0]
        assert text.startswith("➕ Добавлено 12\n\n🔍 Результаты 51-60 из 62+")
        kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert select_buttons(kb)[0] == "select_track:50:6"
        assert any(b.callback_data == "more::0" for row in kb.inline_keyboard for b in row)

    async def test_keeps_sort_and_filter(self, session, more):
        callback, _ = make_callback("more:bitrate_desc:1")
        await cb.cb_more(callback)
        text = callback.message.edit_text.await_args.args[0]
        # lossless filter: only the 12 new 320k tracks qualify → they are page 1
        assert "🔍 Результаты 1-10 из 12+" in text
        kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert any(b.callback_data == "more:bitrate_desc:1" for row in kb.inline_keyboard for b in row)

    async def test_nothing_more_shows_last_page_and_drops_the_button(self, session, more):
        more.n = 0
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        text = callback.message.edit_text.await_args.args[0]
        assert text.startswith("😔 Больше ничего не нашлось\n\n🔍 Результаты 41-50 из 50")
        kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert not any(b.callback_data.startswith("more:") for row in kb.inline_keyboard for b in row)

    async def test_new_tracks_hidden_by_filter(self, session, more):
        more.bitrate = 128
        callback, _ = make_callback("more::1")       # lossless on, nothing qualifies
        await cb.cb_more(callback)
        assert callback.message.edit_text.await_args.args[0].startswith("😔 Больше ничего не нашлось")

    async def test_exhausted_session_answers_without_fetching(self, session, more):
        session.cursor.exhausted |= set(session.cursor.sources)
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        callback.answer.assert_awaited_once_with("Больше ничего не нашлось")
        assert more.calls == 0

    async def test_double_tap_while_loading(self, session, more):
        session.loading = True
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        callback.answer.assert_awaited_once_with("⏳ Уже ищу…")
        assert more.calls == 0

    async def test_expired(self, more):
        search._search_cache.pop(42, None)
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        callback.answer.assert_awaited_once_with("Результаты устарели, выполни поиск снова.")

    async def test_replaced_by_a_new_search_meanwhile(self, session, monkeypatch, make_track):
        async def fake(sess, captcha_handler=None):
            search.store_results(42, [make_track()])     # the user searched again
            return 3
        monkeypatch.setattr(cb, "load_more", fake)
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        callback.message.edit_text.assert_not_called()
        search._search_cache.pop(42, None)

    async def test_failure_is_nothing_more_and_unlocks(self, session, monkeypatch):
        monkeypatch.setattr(cb, "load_more", AsyncMock(side_effect=RuntimeError("vk down")))
        callback, _ = make_callback("more::0")
        await cb.cb_more(callback)
        assert callback.message.edit_text.await_args.args[0].startswith("😔 Больше ничего не нашлось")
        assert session.loading is False

    async def test_page_buttons_offer_more_only_for_the_session_list(self, session):
        callback, _ = make_callback("page:2::0")
        await cb.cb_page(callback)
        kb = callback.message.edit_text.await_args.kwargs["reply_markup"]
        assert any(b.callback_data == "more::0" for row in kb.inline_keyboard for b in row)
        assert callback.message.edit_text.await_args.args[0].startswith("🔍 Результаты 11-20 из 50+")


class TestPopularArtist:
    async def test_artist_mode_session(self, db, monkeypatch, make_track):
        from services import music_search as ms
        monkeypatch.setattr(ms.yt_svc, "search", AsyncMock(return_value=[]))
        monkeypatch.setattr(ms.sp_svc, "search", AsyncMock(return_value=[]))
        monkeypatch.setattr(ms.vk_svc, "search", AsyncMock(return_value=[
            make_track(artist="Кино", title="Кукушка", source="vk", source_id="1", source_rank=5),
            make_track(artist="Кино", title="Группа крови", source="vk", source_id="2", source_rank=0),
        ]))
        callback, _ = make_callback("popular_artist:Кино")
        try:
            await cb.cb_popular_artist(callback)
            sess = search.get_session(42)
            assert sess.rank_query.artist_mode and sess.query == "Кино"
            assert [t.title for t in sess.tracks] == ["Группа крови", "Кукушка"]   # VK's most popular first
            text = callback.message.edit_text.await_args.args[0]
            assert text.startswith("🔍 Результаты 1-2 из 2+")
        finally:
            search._search_cache.pop(42, None)

    async def test_nothing_found(self, db, monkeypatch):
        from services import music_search as ms
        for mod in (ms.yt_svc, ms.vk_svc, ms.sp_svc):
            monkeypatch.setattr(mod, "search", AsyncMock(return_value=[]))
        callback, _ = make_callback("popular_artist:A&B")
        try:
            await cb.cb_popular_artist(callback)
            assert callback.message.edit_text.await_args.args[0] == "😔 Не удалось найти популярные треки A&amp;B"
        finally:
            search._search_cache.pop(42, None)
