"""bot/handlers/recognize.py: link menu, Google share links, video/audio/recognize buttons."""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.models import RecognizedTrack
from bot.handlers import recognize as r


# ── fakes ──────────────────────────────────────────────────────────────────

def make_bot():
    sent = SimpleNamespace(message_id=500, edit_text=AsyncMock())
    return SimpleNamespace(
        send_message=AsyncMock(return_value=sent),
        me=AsyncMock(return_value=SimpleNamespace(username="plcm_bot")),
    )


def make_message(text="https://youtube.com/shorts/x", chat_type="private", is_forum=False, thread=None):
    return SimpleNamespace(
        text=text,
        message_id=10,
        message_thread_id=thread,
        chat=SimpleNamespace(id=777, type=chat_type, is_forum=is_forum),
        from_user=SimpleNamespace(id=42),
        bot=make_bot(),
        delete=AsyncMock(),
    )


def make_callback(data, chat_type="private"):
    bot = make_bot()
    msg = SimpleNamespace(
        message_id=600,
        message_thread_id=None,
        chat=SimpleNamespace(id=777, type=chat_type, is_forum=False),
        bot=bot,
        edit_text=AsyncMock(),
        answer_video=AsyncMock(),
        answer_document=AsyncMock(),
        answer_audio=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=42), answer=AsyncMock(), message=msg, bot=bot)


@pytest.fixture(autouse=True)
def clean_link_cache():
    r._link_urls.clear()
    yield
    r._link_urls.clear()


# ── Google share links ─────────────────────────────────────────────────────

class TestGoogleShare:
    @pytest.mark.parametrize("url", [
        "https://share.google/6ytHTD8O2gX0TrJnL",
        "https://g.co/kgs/abc",
        "https://www.google.com/search?q=x",
        "https://google.de/search?q=x",
        "http://SHARE.GOOGLE/abc",
    ])
    def test_detected(self, url):
        assert r.GOOGLE_SHARE_REGEX.match(url)

    @pytest.mark.parametrize("url", [
        "https://youtube.com/watch?v=1",
        "https://www.google.com/maps/place/x",
        "https://www.instagram.com/reel/x",
        "https://notgoogle.com/search?q=x",
    ])
    def test_not_detected(self, url):
        assert not r.GOOGLE_SHARE_REGEX.match(url)

    def test_query_from_search_url(self):
        assert r._google_search_query("https://www.google.com/search?hl=en&q=%D0%B0+%D1%8F") == "а я"

    def test_query_unwraps_consent_redirect(self):
        url = ("https://consent.google.com/m?continue=https://www.google.com/search"
               "%3Fhl%3Den%26q%3D%25D0%25B0%2B%25D1%258F%26docid%3Dx")
        assert r._google_search_query(url) == "а я"

    def test_share_id_q_is_not_a_search_query(self):
        # www.google.com/share.google?q=<share id> must not be mistaken for the query
        assert r._google_search_query("https://www.google.com/share.google?q=6ytHTD8") is None

    def test_no_query(self):
        assert r._google_search_query("https://www.google.com/search?hl=en") is None

    async def test_resolve_follows_redirect_chain(self, monkeypatch):
        def handler(request: httpx.Request):
            if request.url.host == "share.google":
                return httpx.Response(302, headers={"location": "https://www.google.com/share.google?q=ID"})
            if request.url.path == "/share.google":
                return httpx.Response(301, headers={"location": "https://www.google.com/search?q=%D0%BC%D0%B5%D0%BC"})
            return httpx.Response(200, text="ok")

        real_client = httpx.AsyncClient
        monkeypatch.setattr(r.httpx, "AsyncClient",
                            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        assert await r._resolve_google_query("https://share.google/abc") == "мем"

    async def test_resolve_network_error_is_none(self, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("down")
        real_client = httpx.AsyncClient
        monkeypatch.setattr(r.httpx, "AsyncClient",
                            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
        assert await r._resolve_google_query("https://share.google/abc") is None

    async def test_resolve_direct_search_url_needs_no_request(self, monkeypatch):
        monkeypatch.setattr(r.httpx, "AsyncClient", lambda **kw: pytest.fail("no HTTP expected"))
        assert await r._resolve_google_query("https://www.google.com/search?q=hi") == "hi"


# ── link cache & errors ────────────────────────────────────────────────────

class TestLinkCache:
    def test_round_trip(self):
        tok = r._cache_link("https://a")
        assert len(tok) == 10 and r._link_urls[tok] == "https://a"

    def test_evicts_oldest_beyond_cap(self, monkeypatch):
        monkeypatch.setattr(r, "_LINK_CACHE_MAX", 3)
        toks = [r._cache_link(f"https://{i}") for i in range(5)]
        assert list(r._link_urls) == toks[2:]


class TestLinkDownloadError:
    def test_generic(self):
        assert r._link_download_error("https://tiktok.com/x") == "❌ Не удалось скачать видео по этой ссылке."

    def test_instagram_without_cookies(self, tmp_settings):
        assert "блокирует скачивание без авторизации" in r._link_download_error("https://www.instagram.com/reel/x")

    def test_instagram_with_cookies(self, tmp_settings):
        tmp_settings.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_settings.cookies_path.write_text("x")
        assert "даже с cookies" in r._link_download_error("https://www.instagram.com/reel/x")


# ── handle_link ────────────────────────────────────────────────────────────

class TestHandleLink:
    async def test_sends_single_menu_and_caches_url(self):
        msg = make_message("смотри https://youtube.com/shorts/abc круто")
        await r.handle_link(msg)
        msg.bot.send_message.assert_awaited_once()
        kwargs = msg.bot.send_message.await_args.kwargs
        assert kwargs["text"] == "🔗 Что сделать с этой ссылкой?"
        datas = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
        token = datas[0].split(":", 1)[1]
        assert r._link_urls[token] == "https://youtube.com/shorts/abc"
        assert datas == [f"lv:{token}", f"lva:{token}", f"lr:{token}"]

    async def test_no_background_download_or_recognition(self, monkeypatch):
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(side_effect=AssertionError("no auto download")))
        await r.handle_link(make_message())

    async def test_google_link_gets_hint_not_menu(self, monkeypatch):
        monkeypatch.setattr(r, "_resolve_google_query", AsyncMock(return_value="а я латыш мем"))
        msg = make_message("https://share.google/xyz")
        await r.handle_link(msg)
        text = msg.bot.send_message.await_args.kwargs["text"]
        assert "поиск Google «а я латыш мем»" in text
        assert "reply_markup" not in msg.bot.send_message.await_args.kwargs
        assert r._link_urls == {}

    async def test_google_link_without_query(self, monkeypatch):
        monkeypatch.setattr(r, "_resolve_google_query", AsyncMock(return_value=None))
        msg = make_message("https://share.google/xyz")
        await r.handle_link(msg)
        assert "ссылка на поиск Google, а не" in msg.bot.send_message.await_args.kwargs["text"]

    async def test_forum_topic_message_is_deleted_and_menu_goes_to_general(self):
        msg = make_message(chat_type="supergroup", is_forum=True, thread=5)
        await r.handle_link(msg)
        msg.delete.assert_awaited_once()
        assert msg.bot.send_message.await_args.kwargs["message_thread_id"] is None

    async def test_private_thread_is_kept(self):
        msg = make_message(thread=9)
        await r.handle_link(msg)
        assert msg.bot.send_message.await_args.kwargs["message_thread_id"] == 9


# ── buttons ────────────────────────────────────────────────────────────────

class TestExpiredToken:
    @pytest.mark.parametrize("handler,data", [
        (r.cb_link_video, "lv:nope"),
        (r.cb_link_video_res, "lvr:nope:720"),
        (r.cb_link_file, "lvf:nope"),
        (r.cb_link_audio, "lva:nope"),
        (r.cb_link_recognize, "lr:nope"),
    ])
    async def test_alerts_and_does_nothing(self, handler, data):
        cb = make_callback(data)
        await handler(cb)
        cb.answer.assert_awaited_once_with("Ссылка устарела — пришлите её заново.", show_alert=True)
        cb.message.edit_text.assert_not_called()


class TestVideoButtons:
    async def test_lv_probes_sizes_then_shows_picker(self, monkeypatch):
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "probe_video_sizes", AsyncMock(return_value={"best": 2 * 1024 * 1024}))
        cb = make_callback(f"lv:{tok}")
        await r.cb_link_video(cb)
        last = cb.message.edit_text.await_args
        assert last.args[0] == "🎬 Что скачать?"
        assert last.kwargs["reply_markup"].inline_keyboard[0][0].text == "🏆 Лучшее (~2.0MB)"

    async def test_lv_probe_crash_still_shows_picker(self, monkeypatch):
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "probe_video_sizes", AsyncMock(side_effect=RuntimeError("boom")))
        cb = make_callback(f"lv:{tok}")
        await r.cb_link_video(cb)
        assert cb.message.edit_text.await_args.args[0] == "🎬 Что скачать?"

    async def test_720_sends_video_with_signature_and_cleans_up(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 10)
        tok = r._cache_link("https://v")
        dl = AsyncMock(return_value=str(f))
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", dl)
        cb = make_callback(f"lvr:{tok}:720")
        await r.cb_link_video_res(cb)
        dl.assert_awaited_once_with("https://v", max_height=720)
        assert cb.message.answer_video.await_args.kwargs["caption"] == "🎬 720p\n\n🤖 @plcm_bot"
        cb.message.delete.assert_awaited_once()
        assert not f.exists()

    async def test_best_in_group_has_no_signature(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        dl = AsyncMock(return_value=str(f))
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", dl)
        cb = make_callback(f"lvr:{tok}:best", chat_type="supergroup")
        await r.cb_link_video_res(cb)
        dl.assert_awaited_once_with("https://v", max_height=None)
        assert cb.message.answer_video.await_args.kwargs["caption"] == "🎬 Лучшее"

    async def test_as_file_sends_document(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", AsyncMock(return_value=str(f)))
        cb = make_callback(f"lvf:{tok}")
        await r.cb_link_file(cb)
        assert cb.message.answer_document.await_args.kwargs["caption"] == "🎬 Лучшее (файл)\n\n🤖 @plcm_bot"
        cb.message.answer_video.assert_not_called()

    async def test_too_big_is_refused_and_removed(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x" * 100)
        monkeypatch.setattr(r.settings, "telegram_api_base_url", "")   # 50 MB cloud limit
        monkeypatch.setattr(r.os.path, "getsize", lambda p: 60 * 1024 * 1024)
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", AsyncMock(return_value=str(f)))
        cb = make_callback(f"lvr:{tok}:best")
        await r.cb_link_video_res(cb)
        assert "слишком большое (>50MB)" in cb.message.edit_text.await_args.args[0]
        cb.message.answer_video.assert_not_called()
        assert not f.exists()

    async def test_download_failure_message(self, monkeypatch):
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", AsyncMock(return_value=None))
        cb = make_callback(f"lvr:{tok}:360")
        await r.cb_link_video_res(cb)
        assert cb.message.edit_text.await_args.args[0].startswith("❌ Не удалось скачать")

    async def test_send_error_still_removes_file(self, monkeypatch, tmp_path):
        f = tmp_path / "v.mp4"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_video_from_url", AsyncMock(return_value=str(f)))
        cb = make_callback(f"lvr:{tok}:720")
        cb.message.answer_video.side_effect = RuntimeError("telegram down")
        await r.cb_link_video_res(cb)
        assert "❌ Ошибка: telegram down" == cb.message.edit_text.await_args.args[0]
        assert not f.exists()


class TestAudioButton:
    async def test_sends_mp3_with_signature(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=str(f)))
        cb = make_callback(f"lva:{tok}")
        await r.cb_link_audio(cb)
        assert cb.message.answer_audio.await_args.kwargs["caption"] == "🤖 @plcm_bot"
        assert not f.exists()

    async def test_group_has_empty_caption(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=str(f)))
        cb = make_callback(f"lva:{tok}", chat_type="group")
        await r.cb_link_audio(cb)
        assert cb.message.answer_audio.await_args.kwargs["caption"] == ""

    async def test_failure(self, monkeypatch):
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=None))
        cb = make_callback(f"lva:{tok}")
        await r.cb_link_audio(cb)
        assert cb.message.edit_text.await_args.args[0] == "❌ Не удалось извлечь звук."


class TestRecognizeButton:
    async def test_download_failure_uses_link_error(self, monkeypatch, tmp_settings):
        tok = r._cache_link("https://www.instagram.com/reel/x")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=None))
        cb = make_callback(f"lr:{tok}")
        await r.cb_link_recognize(cb)
        last = cb.message.edit_text.await_args
        assert "Instagram" in last.args[0] and last.kwargs["parse_mode"] == "Markdown"

    async def test_success_reuses_menu_message_as_status(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=str(f)))
        ras = AsyncMock()
        monkeypatch.setattr(r, "_recognize_and_search", ras)
        cb = make_callback(f"lr:{tok}")
        await r.cb_link_recognize(cb)
        args, kwargs = ras.await_args
        assert args == (cb.message, str(f)) and kwargs == {"status": cb.message, "user_id": 42}

    async def test_nothing_recognized_edits_same_message_and_removes_audio(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=str(f)))
        monkeypatch.setattr(r.recognizer, "recognize", AsyncMock(return_value=None))
        cb = make_callback(f"lr:{tok}")
        await r.cb_link_recognize(cb)
        texts = [c.args[0] for c in cb.message.edit_text.await_args_list]
        assert texts == ["⏳ Скачиваю звук для распознавания...", "🎧 Распознаю трек...",
                         "😔 Не удалось распознать музыку в этом видео."]
        cb.message.bot.send_message.assert_not_called()
        assert not f.exists()

    async def test_recognized_but_no_full_version(self, monkeypatch, tmp_path):
        f = tmp_path / "a.mp3"
        f.write_bytes(b"x")
        tok = r._cache_link("https://v")
        monkeypatch.setattr(r.yt_svc, "download_from_url", AsyncMock(return_value=str(f)))
        monkeypatch.setattr(r.recognizer, "recognize", AsyncMock(return_value=RecognizedTrack(title="T", artist="A")))
        monkeypatch.setattr(r.yt_svc, "search", AsyncMock(return_value=[]))
        from services import spotify, vk_music
        monkeypatch.setattr(vk_music, "search", AsyncMock(return_value=[]))
        monkeypatch.setattr(spotify, "search", AsyncMock(side_effect=RuntimeError("no creds")))
        cb = make_callback(f"lr:{tok}")
        await r.cb_link_recognize(cb)
        assert cb.message.edit_text.await_args.args[0] == "🎵 <b>A – T</b>\nПолная версия не найдена."
        assert not f.exists()


class TestRecognizeFromMessage:
    async def test_voice_flow_sends_new_status_and_hint(self, monkeypatch, tmp_path):
        f = tmp_path / "v.ogg"
        f.write_bytes(b"x")
        monkeypatch.setattr(r.recognizer, "recognize", AsyncMock(return_value=None))
        msg = make_message()
        await r._recognize_and_search(msg, str(f))
        msg.bot.send_message.assert_awaited_once()
        status = msg.bot.send_message.return_value
        status.edit_text.assert_awaited_once_with("😔 Ничего не нашлось. Попробуй более длинный фрагмент.")
        assert not os.path.exists(f)


# ── which texts reach handle_link (real aiogram filters) ───────────────────

from datetime import datetime  # noqa: E402

from aiogram.types import Chat, Message as TgMessage  # noqa: E402


async def link_handler_accepts(text):
    handler = next(h for h in r.router.message.handlers if h.callback is r.handle_link)
    ok, _ = await handler.check(TgMessage(message_id=1, date=datetime.now(), chat=Chat(id=1, type="private"), text=text))
    return ok


class TestLinkRouting:
    @pytest.mark.parametrize("text", [
        "https://youtube.com/shorts/x",
        "Check out this video, \"а я латыш мем\" https://share.google/6ytHTD8O2gX0TrJnL",
        "Посмотри это видео на TikTok https://vm.tiktok.com/ZM123/",
    ])
    async def test_links_anywhere_in_text(self, text):
        assert await link_handler_accepts(text)

    @pytest.mark.parametrize("text", [
        "https://vk.com/music/playlist/-147845620_2949_abc",
        "https://m.vk.com/audio?z=audio_playlist-1_2",
        "Imagine Dragons Believer",
    ])
    async def test_not_links_or_vk_playlists(self, text):
        assert not await link_handler_accepts(text)
