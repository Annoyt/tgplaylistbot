"""Web dashboard auth: JWT service, login hardening, every admin route behind auth."""

from datetime import timedelta

import jwt
import pytest

from app.auth import router as auth_router
from app.auth import service
from config import settings


@pytest.fixture(autouse=True)
def reset_login_failures():
    auth_router._failures.clear()
    yield
    auth_router._failures.clear()


# ── service ────────────────────────────────────────────────────────────────

class TestService:
    def test_password_round_trip(self):
        h = service.hash_password("s3cret")
        assert service.verify_password("s3cret", h)
        assert not service.verify_password("wrong", h)

    def test_token_round_trip(self):
        tok = service.create_access_token({"sub": "admin", "role": "admin"})
        payload = service.decode_access_token(tok)
        assert payload["sub"] == "admin" and payload["role"] == "admin" and "exp" in payload

    def test_expired_token_rejected(self):
        tok = service.create_access_token({"sub": "admin"}, expires_delta=timedelta(seconds=-1))
        assert service.decode_access_token(tok) is None

    def test_token_from_other_key_rejected(self):
        forged = jwt.encode({"sub": "admin"}, "some-other-secret-key-123456", algorithm="HS256")
        assert service.decode_access_token(forged) is None

    def test_alg_none_rejected(self):
        unsigned = jwt.encode({"sub": "admin"}, key=None, algorithm="none")
        assert service.decode_access_token(unsigned) is None

    def test_garbage_rejected(self):
        assert service.decode_access_token("not.a.jwt") is None

    def test_default_secret_is_unsafe_and_blocks_tokens(self, monkeypatch):
        monkeypatch.setattr(settings, "secret_key", service.DEFAULT_SECRET_KEY)
        assert not service.secret_key_is_safe()
        forged = jwt.encode({"sub": "admin"}, service.DEFAULT_SECRET_KEY, algorithm="HS256")
        assert service.decode_access_token(forged) is None
        with pytest.raises(RuntimeError):
            service.create_access_token({"sub": "admin"})

    @pytest.mark.parametrize("key,safe", [("x" * 15, False), ("x" * 16, True)])
    def test_short_secret_is_unsafe(self, monkeypatch, key, safe):
        monkeypatch.setattr(settings, "secret_key", key)
        assert service.secret_key_is_safe() is safe

    def test_env_password_default_detection(self, monkeypatch):
        monkeypatch.setattr(settings, "admin_password", service.DEFAULT_ADMIN_PASSWORD)
        assert service.env_password_is_default()
        monkeypatch.setattr(settings, "admin_password", "something-else")
        assert not service.env_password_is_default()


# ── /login ─────────────────────────────────────────────────────────────────

async def login(client, username="admin", password="test-pass"):
    return await client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


class TestLogin:
    async def test_success_sets_httponly_cookie(self, client):
        r = await login(client)
        assert r.status_code == 303 and r.headers["location"] == "/admin/"
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("access_token=") and "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert service.decode_access_token(r.cookies["access_token"])["sub"] == "admin"

    @pytest.mark.parametrize("username,password", [("admin", "nope"), ("root", "test-pass"), ("", "")])
    async def test_bad_credentials(self, client, username, password):
        r = await login(client, username, password)
        assert r.status_code == 401 and "Неверный логин или пароль" in r.text

    async def test_file_part_instead_of_password_is_401_not_500(self, client):
        r = await client.post("/login", data={"username": "admin"}, files={"password": ("p.txt", b"test-pass")},
                              follow_redirects=False)
        assert r.status_code == 401

    async def test_db_password_overrides_env(self, client, db):
        from app.db.database import get_db_ctx
        async with get_db_ctx() as conn:
            await conn.execute("INSERT INTO bot_settings (key, value) VALUES ('admin_password_hash', ?)",
                               (service.hash_password("from-db-pass"),))
            await conn.commit()
        assert (await login(client, password="test-pass")).status_code == 401
        assert (await login(client, password="from-db-pass")).status_code == 303

    async def test_locked_while_secret_key_is_default(self, client, monkeypatch):
        monkeypatch.setattr(settings, "secret_key", service.DEFAULT_SECRET_KEY)
        r = await login(client)
        assert r.status_code == 503 and "SECRET_KEY" in r.text

    async def test_locked_while_env_password_default_and_no_db_password(self, client, monkeypatch):
        monkeypatch.setattr(settings, "admin_password", service.DEFAULT_ADMIN_PASSWORD)
        r = await login(client, password=service.DEFAULT_ADMIN_PASSWORD)
        assert r.status_code == 503 and "ADMIN_PASSWORD" in r.text

    async def test_rate_limited_after_max_failures(self, client):
        for _ in range(auth_router.LOGIN_MAX_FAILURES):
            assert (await login(client, password="bad")).status_code == 401
        r = await login(client)                       # even the right password
        assert r.status_code == 429

    async def test_failures_expire_after_window(self, client, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr(auth_router.time, "monotonic", lambda: clock[0])
        for _ in range(auth_router.LOGIN_MAX_FAILURES):
            await login(client, password="bad")
        assert (await login(client)).status_code == 429
        clock[0] += auth_router.LOGIN_WINDOW_SEC + 1
        assert (await login(client)).status_code == 303

    async def test_success_resets_failures(self, client):
        for _ in range(auth_router.LOGIN_MAX_FAILURES - 1):
            await login(client, password="bad")
        assert (await login(client)).status_code == 303
        assert (await login(client, password="bad")).status_code == 401   # counter restarted

    async def test_logout_clears_cookie(self, client):
        r = await client.get("/logout", follow_redirects=False)
        assert r.status_code == 303 and 'access_token=""' in r.headers["set-cookie"]


# ── admin routes require auth ──────────────────────────────────────────────

ADMIN_ROUTES = [
    ("GET", "/admin/"),
    ("POST", "/admin/settings"),
    ("POST", "/admin/password"),
    ("GET", "/admin/musicbot"),
    ("POST", "/admin/musicbot"),
    ("GET", "/admin/weatherbot"),
    ("POST", "/admin/weatherbot"),
    ("POST", "/admin/verify/telegram"),
    ("POST", "/admin/verify/weather"),
]


class TestAdminAuth:
    @pytest.mark.parametrize("method,path", ADMIN_ROUTES)
    async def test_redirects_to_login_without_cookie(self, client, method, path):
        r = await client.request(method, path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/login"

    @pytest.mark.parametrize("method,path", ADMIN_ROUTES)
    async def test_forged_default_key_cookie_rejected(self, client, monkeypatch, method, path):
        forged = jwt.encode({"sub": "admin", "role": "admin"}, service.DEFAULT_SECRET_KEY, algorithm="HS256")
        monkeypatch.setattr(settings, "secret_key", service.DEFAULT_SECRET_KEY)
        client.cookies.set("access_token", forged)
        r = await client.request(method, path, follow_redirects=False)
        assert r.status_code == 307

    async def test_cookie_upload_without_auth_writes_nothing(self, client, tmp_settings):
        r = await client.post("/admin/musicbot/cookies", files={"cookies_file": ("c.txt", b"# Netscape HTTP Cookie File\n")},
                              follow_redirects=False)
        assert r.status_code == 307
        assert not tmp_settings.cookies_path.exists()

    async def test_logged_in_admin_reaches_dashboard(self, client):
        await login(client)
        r = await client.get("/admin/musicbot", follow_redirects=False)
        assert r.status_code == 200


class TestAuthenticatedAdmin:
    @pytest.fixture
    async def admin(self, client):
        assert (await login(client)).status_code == 303
        return client

    async def test_cookie_upload_accepts_netscape(self, admin, tmp_settings):
        body = b"# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tx\n"
        r = await admin.post("/admin/musicbot/cookies", files={"cookies_file": ("c.txt", body)}, follow_redirects=False)
        assert r.headers["location"].endswith("cookies=ok")
        assert tmp_settings.cookies_path.read_bytes() == body

    async def test_cookie_upload_rejects_other_formats(self, admin, tmp_settings):
        r = await admin.post("/admin/musicbot/cookies", files={"cookies_file": ("c.json", b'{"a": 1}')},
                             follow_redirects=False)
        assert r.headers["location"].endswith("cookies=badformat")
        assert not tmp_settings.cookies_path.exists()

    @pytest.mark.parametrize("new,confirm", [("short", "short"), ("longenough", "different"), ("", "")])
    async def test_password_change_rejects_bad_input(self, admin, new, confirm):
        r = await admin.post("/admin/password", data={"new_password": new, "confirm_password": confirm},
                             follow_redirects=False)
        assert r.headers["location"] == "/admin/?pwd_error=1"

    async def test_password_change_takes_effect(self, admin):
        r = await admin.post("/admin/password", data={"new_password": "brand-new-pass", "confirm_password": "brand-new-pass"},
                             follow_redirects=False)
        assert r.headers["location"] == "/admin/?pwd_saved=1"
        assert (await login(admin, password="test-pass")).status_code == 401
        assert (await login(admin, password="brand-new-pass")).status_code == 303

    async def test_musicbot_settings_persist(self, admin):
        await admin.post("/admin/musicbot", data={"vk_token": "tok-123", "default_quality": "flac"}, follow_redirects=False)
        from app.db.database import get_db_ctx
        async with get_db_ctx() as conn:
            rows = {r["key"]: r["value"] for r in await (await conn.execute("SELECT key, value FROM bot_settings")).fetchall()}
        assert rows["vk_token"] == "tok-123" and rows["default_quality"] == "flac" and rows["spotify_client_id"] == ""

    async def test_verify_telegram_needs_token(self, admin):
        r = await admin.post("/admin/verify/telegram", data={})
        assert r.json() == {"ok": False, "error": "Токен не предоставлен"}


class TestPublicPages:
    async def test_health(self, client):
        r = await client.get("/health")
        assert r.json() == {"status": "ok", "service": "musicbot-platform"}

    @pytest.mark.parametrize("path", ["/", "/login"])
    async def test_pages_render(self, client, path):
        r = await client.get(path)
        assert r.status_code == 200 and "html" in r.headers["content-type"]

    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    async def test_api_docs_disabled(self, client, path):
        assert (await client.get(path)).status_code == 404
