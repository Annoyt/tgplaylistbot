"""Bot admin rights, /setadmin, global settings parsing, VK credential handlers, captcha."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.auth import service
from app.db.database import get_db_ctx
from bot.handlers import admin, auth
from config import settings
from services.vk_captcha import CaptchaManager


def make_message(text, user_id=42):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, username="u"),
        chat=SimpleNamespace(id=user_id, type="private"),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )


async def set_admin(user_id):
    async with get_db_ctx() as conn:
        await conn.execute("INSERT INTO users (id, username, is_admin) VALUES (?, 'x', 1)", (user_id,))
        await conn.commit()


async def bot_setting(key):
    async with get_db_ctx() as conn:
        row = await (await conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))).fetchone()
    return row["value"] if row else None


# ── admin rights ───────────────────────────────────────────────────────────

class TestIsBotAdmin:
    async def test_unknown_user(self, db):
        assert not await admin.is_bot_admin(1)

    async def test_admin_flag(self, db):
        await set_admin(7)
        assert await admin.is_bot_admin(7)


class TestAdminPassword:
    async def test_env_password(self, db):
        assert await admin.admin_password_ok("test-pass")
        assert not await admin.admin_password_ok("test-pas")

    async def test_default_env_password_never_works(self, db, monkeypatch):
        monkeypatch.setattr(settings, "admin_password", service.DEFAULT_ADMIN_PASSWORD)
        assert not await admin.admin_password_ok(service.DEFAULT_ADMIN_PASSWORD)

    async def test_dashboard_password_wins(self, db, monkeypatch):
        monkeypatch.setattr(settings, "admin_password", service.DEFAULT_ADMIN_PASSWORD)
        await admin.update_global_setting("admin_password_hash", service.hash_password("dash-pass"))
        assert await admin.admin_password_ok("dash-pass")
        assert not await admin.admin_password_ok("test-pass")


class TestSetAdmin:
    async def test_correct_password_grants_and_hides_message(self, db):
        msg = make_message("/setadmin test-pass")
        await admin.cmd_setadmin(msg)
        msg.delete.assert_awaited_once()
        assert await admin.is_bot_admin(42)

    async def test_wrong_password_silently_ignored(self, db):
        msg = make_message("/setadmin guess")
        await admin.cmd_setadmin(msg)
        msg.delete.assert_awaited_once()
        msg.answer.assert_not_called()
        assert not await admin.is_bot_admin(42)

    async def test_default_password_rejected(self, db, monkeypatch):
        monkeypatch.setattr(settings, "admin_password", service.DEFAULT_ADMIN_PASSWORD)
        await admin.cmd_setadmin(make_message(f"/setadmin {service.DEFAULT_ADMIN_PASSWORD}"))
        assert not await admin.is_bot_admin(42)

    async def test_no_argument_does_nothing(self, db):
        msg = make_message("/setadmin")
        await admin.cmd_setadmin(msg)
        msg.delete.assert_not_called()

    async def test_delete_failure_does_not_block(self, db):
        msg = make_message("/setadmin test-pass")
        msg.delete.side_effect = RuntimeError("no rights")
        await admin.cmd_setadmin(msg)
        assert await admin.is_bot_admin(42)


# ── global settings ────────────────────────────────────────────────────────

class TestGlobalSettings:
    async def test_defaults_when_empty(self, db):
        s = await admin.get_global_settings()
        assert (s.vote_threshold_pct, s.vote_interval_sec, s.download_delay_sec, s.msg_ttl_days, s.forward_mode) == \
            (25, 60, 3, 7, "resend")

    async def test_valid_values_parsed(self, db):
        for k, v in {"vote_threshold_pct": "40", "download_delay_sec": "0", "forward_mode": "forward"}.items():
            await admin.update_global_setting(k, v)
        s = await admin.get_global_settings()
        assert s.vote_threshold_pct == 40 and s.download_delay_sec == 0 and s.forward_mode == "forward"

    @pytest.mark.parametrize("bad", ["None", "abc", "-5", "", "1.5"])
    async def test_bad_stored_values_fall_back(self, db, bad):
        await admin.update_global_setting("msg_ttl_days", bad)
        await admin.update_global_setting("forward_mode", bad)
        s = await admin.get_global_settings()
        assert s.msg_ttl_days == 7 and s.forward_mode == "resend"

    @pytest.mark.parametrize("key,value,ok", [
        ("msg_ttl_days", "3", True), ("msg_ttl_days", " 3 ", True), ("msg_ttl_days", "x", False),
        ("msg_ttl_days", "-1", False), ("forward_mode", "forward", True), ("forward_mode", "spam", False),
        ("nope", "1", False),
    ])
    def test_setting_error(self, key, value, ok):
        assert (admin.setting_error(key, value) is None) is ok

    async def test_set_global_rejects_bad_value(self, db):
        await set_admin(42)
        msg = make_message("/set_global msg_ttl_days soon")
        await admin.cmd_set_global(msg)
        assert "Неверное значение" in msg.answer.await_args.args[0]
        assert await bot_setting("msg_ttl_days") is None

    async def test_set_global_saves_valid_value(self, db):
        await set_admin(42)
        await admin.cmd_set_global(make_message("/set_global msg_ttl_days 3"))
        assert await bot_setting("msg_ttl_days") == "3"

    async def test_set_global_ignores_non_admin(self, db):
        msg = make_message("/set_global msg_ttl_days 3")
        await admin.cmd_set_global(msg)
        msg.answer.assert_not_called()


class TestDashboardSettingsForm:
    async def test_partial_form_keeps_dashboard_alive(self, client):
        assert (await client.post("/login", data={"username": "admin", "password": "test-pass"})).status_code in (200, 303)
        await client.post("/admin/settings", data={"vote_threshold_pct": "30"}, follow_redirects=False)
        assert await bot_setting("vote_threshold_pct") == "30"
        assert await bot_setting("msg_ttl_days") is None          # missing field not stored as "None"
        await client.post("/admin/settings", data={"msg_ttl_days": "abc", "forward_mode": "x"}, follow_redirects=False)
        assert await bot_setting("msg_ttl_days") is None
        assert (await client.get("/admin/", follow_redirects=False)).status_code == 200


# ── VK credentials (bot) ───────────────────────────────────────────────────

class TestVkCredentialHandlers:
    async def test_non_admin_cannot_replace_token(self, db):
        msg = make_message("access_token=vk1.a.evil")
        await auth.handle_vk_token_link(msg)
        msg.delete.assert_awaited_once()
        assert "только администратор" in msg.answer.await_args.args[0]
        assert await bot_setting("vk_token") is None

    async def test_admin_replaces_token_and_message_is_hidden(self, db):
        await set_admin(42)
        msg = make_message("access_token=vk1.a.good&user_id=5")
        await auth.handle_vk_token_link(msg)
        msg.delete.assert_awaited_once()
        assert await bot_setting("vk_token") == "vk1.a.good"

    async def test_non_admin_vklogin_changes_nothing(self, db):
        await admin.update_global_setting("vk_token", "keep-me")
        msg = make_message("/vklogin +7999 secret")
        await auth.cmd_vklogin(msg)
        msg.delete.assert_awaited_once()
        assert await bot_setting("vk_token") == "keep-me"
        assert await bot_setting("vk_password") is None

    async def test_admin_vklogin(self, db):
        await set_admin(42)
        await admin.update_global_setting("vk_token", "old")
        await auth.cmd_vklogin(make_message("/vklogin +7999 secret"))
        assert await bot_setting("vk_token") is None and await bot_setting("vk_login") == "+7999"

    async def test_vklogin_bad_format(self, db):
        msg = make_message("/vklogin onlyone")
        await auth.cmd_vklogin(msg)
        assert "Неверный формат" in msg.answer.await_args.args[0]
        msg.delete.assert_not_called()


# ── captcha ────────────────────────────────────────────────────────────────

class FakeCaptcha:
    def get_url(self):
        return "https://vk.com/captcha.png"

    def try_again(self, answer):
        return f"retried:{answer}"


async def wait_for_session(mgr, user_id):
    for _ in range(200):
        if user_id in mgr.active_sessions:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("captcha session never registered")


class TestCaptcha:
    async def test_solved_in_worker_thread_and_session_cleared(self):
        mgr = CaptchaManager()
        bot = SimpleNamespace(send_photo=AsyncMock())
        handler = mgr.get_captcha_handler(bot, chat_id=1, user_id=99)
        task = asyncio.create_task(asyncio.to_thread(handler, FakeCaptcha()))   # vk_api runs it in a thread
        await wait_for_session(mgr, 99)
        assert mgr.solve_captcha(99, "x7k2")
        assert await task == "retried:x7k2"
        assert 99 not in mgr.active_sessions
        bot.send_photo.assert_awaited_once()

    async def test_timeout_clears_session(self, monkeypatch):
        import services.vk_captcha as vc
        monkeypatch.setattr(vc, "CAPTCHA_TIMEOUT_SEC", 0.05)
        mgr = CaptchaManager()
        handler = mgr.get_captcha_handler(SimpleNamespace(send_photo=AsyncMock()), 1, 99)
        assert await asyncio.to_thread(handler, FakeCaptcha()) is None
        assert mgr.active_sessions == {}

    async def test_send_failure_does_not_hang(self):
        mgr = CaptchaManager()
        bot = SimpleNamespace(send_photo=AsyncMock(side_effect=RuntimeError("blocked")))
        handler = mgr.get_captcha_handler(bot, 1, 99)
        assert await asyncio.wait_for(asyncio.to_thread(handler, FakeCaptcha()), timeout=5) is None
        assert mgr.active_sessions == {}

    def test_solve_without_session(self):
        assert CaptchaManager().solve_captcha(1, "x") is False

    def test_handler_must_be_created_inside_the_loop(self):
        with pytest.raises(RuntimeError):
            CaptchaManager().get_captcha_handler(None, 1, 1)
