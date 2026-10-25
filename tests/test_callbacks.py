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
    search._search_cache[42] = tracks
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
        search._search_cache[42] = [make_track(artist="A&B", title="I <3 you")]
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
        search._search_cache[42] = [make_track(artist="A" * 150, title="T" * 150)]
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
