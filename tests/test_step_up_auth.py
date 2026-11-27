"""Bộ kiểm thử chuyên sâu cho Step-up Authentication (60s sliding idle window, zero data injection, dual re-auth)."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.features.auth import dependencies as auth_deps_module
from app.features.auth import router as auth_router_module
from app.features.auth.dependencies import STEP_UP_COOKIE_NAME, STEP_UP_DETAIL
from app.web.app import create_dashboard_app


@pytest.mark.asyncio
async def test_zero_server_side_data_injection_and_jinja_only_comments(app_container, fake_db, fake_bot):
    """Trang /dashboard KHÔNG chèn danh sách admin, phân quyền, lịch sử chat hay form/nút nhạy cảm vào HTML."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await app_container.auth_service.create_user("secret_second_admin", "SecondPass123!", role="admin")
    await app_container.permission_service.upsert_permission(
        "987654321098765432",
        label="SecretDiscordAdminLabel",
        is_admin=True,
        can_dashboard=True,
        can_status=True,
    )
    await app_container.session_repo.save_turn(
        777888,
        "SECRET_CHAT_MESSAGE_SHOULD_NOT_BE_IN_HTML",
        "SECRET_BOT_REPLY_SHOULD_NOT_BE_IN_HTML",
        None,
        "gemini-2.5-flash",
    )

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        dash = client.get("/dashboard")
        assert dash.status_code == 200
        html = dash.text

        # Zero sensitive records in HTML or inline scripts
        assert "secret_second_admin" not in html
        assert "987654321098765432" not in html
        assert "SecretDiscordAdminLabel" not in html
        assert "SECRET_CHAT_MESSAGE_SHOULD_NOT_BE_IN_HTML" not in html
        assert "SECRET_BOT_REPLY_SHOULD_NOT_BE_IN_HTML" not in html

        # Zero pre-rendered sensitive forms/tables/action buttons in locked HTML
        assert 'id="form-add-discord-perm"' not in html
        assert 'id="form-add-user"' not in html
        assert 'id="form-search-sessions"' not in html
        assert 'id="discord-perms-tbody"' not in html
        assert 'id="users-tbody"' not in html
        assert 'id="sessions-search-tbody"' not in html

        # Step-up modal & dynamic mount roots exist
        assert 'id="stepup-modal"' in html
        assert 'id="perms-content-root"' in html
        assert 'id="sessions-content-root"' in html

    # Verify Jinja2 {# ... #} comments ONLY in updated/new templates (no HTML <!-- comments)
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    for rel_path in (
        "dashboard.html",
        "partials/step_up_modal.html",
        "partials/permissions_tab.html",
        "partials/sessions_tab.html",
    ):
        source = (templates_dir / rel_path).read_text(encoding="utf-8")
        assert "<!--" not in source, f"HTML comment found in {rel_path}; only Jinja2 {{# ... #}} comments allowed"


@pytest.mark.asyncio
async def test_every_sensitive_read_and_mutation_requires_step_up_auth(app_container, fake_db, fake_bot):
    """Mọi endpoint GET/POST/PUT/DELETE của Phân quyền & Admin và Tra cứu phiên chat đều trả 403 STEP_UP_REQUIRED khi chưa xác thực."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await app_container.session_repo.save_turn(777888, "hello", "world", None, "gemini-2.5-flash")

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        sensitive_calls = [
            ("GET", "/api/permissions/discord", None),
            ("POST", "/api/permissions/discord", {"user_id": "222333444555666777", "label": "Forged", "can_status": True}),
            ("DELETE", "/api/permissions/discord/222333444555666777", None),
            ("GET", "/api/permissions/blacklist", None),
            ("POST", "/api/permissions/blacklist", {"discord_user_id": "222333444555666777", "reason": "Forged"}),
            ("DELETE", "/api/permissions/blacklist/222333444555666777", None),
            ("GET", "/api/auth/login-history", None),
            ("GET", "/api/permissions/users", None),
            ("POST", "/api/permissions/users", {"username": "forged1", "password": "Password123!", "role": "operator"}),
            ("PUT", "/api/permissions/users/admin/password", {"password": "Password456!"}),
            ("DELETE", "/api/permissions/users/admin", None),
            ("GET", "/api/sessions/filters", None),
            ("GET", "/api/sessions/search", None),
            ("GET", "/api/sessions/nonexistent", None),
        ]
        for method, path, body in sensitive_calls:
            res = client.request(method, path, json=body)
            assert res.status_code == 403, f"{method} {path} returned {res.status_code} instead of 403"
            assert res.json() == {"ok": False, "code": "LOCKED", "locked": True}, f"Unexpected payload for {method} {path}: {res.text}"
            set_cookie = res.headers.get("set-cookie", "").lower()
            assert STEP_UP_COOKIE_NAME in set_cookie and "max-age=0" in set_cookie


@pytest.mark.asyncio
async def test_stateless_60s_sliding_window_no_background_refresh_and_session_binding(
    app_container, fake_db, fake_bot, monkeypatch
):
    """Kiểm tra cửa sổ trượt 60s: chỉ trượt trên endpoint nhạy cảm, không trượt khi poll /api/status, gắn chặt user_id+session_id."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")

    fake_now = [1_800_000_000.0]
    monkeypatch.setattr(auth_deps_module.time, "time", lambda: fake_now[0])
    monkeypatch.setattr(auth_router_module.time, "time", lambda: fake_now[0])

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client, TestClient(web_app) as other_session_client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        assert other_session_client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        # Step-up bằng mật khẩu
        su = client.post("/api/auth/step-up/password", json={"password": "AdminPass123!"})
        assert su.status_code == 200
        set_cookie = su.headers.get("set-cookie", "").lower()
        assert STEP_UP_COOKIE_NAME in set_cookie
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert "max-age=60" in set_cookie

        stolen_stepup_cookie = client.cookies.get(STEP_UP_COOKIE_NAME)
        assert stolen_stepup_cookie

        # Cookie kiwikiwi_stepup gắn chặt với session_id: sao chép sang phiên khác của cùng user vẫn bị chặn 403
        other_session_client.cookies.set(STEP_UP_COOKIE_NAME, stolen_stepup_cookie)
        stolen_res = other_session_client.get("/api/permissions/users")
        assert stolen_res.status_code == 403
        assert stolen_res.json() == {"ok": False, "code": "LOCKED", "locked": True}

        # Sau 40s -> gọi endpoint nhạy cảm => 200 OK và trượt last_active_at = now
        fake_now[0] += 40.0
        ok_slide = client.get("/api/permissions/discord")
        assert ok_slide.status_code == 200
        assert STEP_UP_COOKIE_NAME in ok_slide.headers.get("set-cookie", "").lower()

        # Sau thêm 40s (tổng 80s từ đầu, nhưng chỉ 40s từ thao tác nhạy cảm gần nhất) => vẫn 200 OK
        fake_now[0] += 40.0
        still_ok = client.get("/api/sessions/search")
        assert still_ok.status_code == 200

        # Gọi polling nền (/api/status, /api/config, /api/me, /api/auth/step-up/status) KHÔNG được trượt last_active_at
        for _ in range(3):
            fake_now[0] += 25.0  # tổng +75s > 60s
            status_res = client.get("/api/status")
            assert status_res.status_code == 200
            assert STEP_UP_COOKIE_NAME not in status_res.headers.get("set-cookie", "").lower()

        # Quá 60s không gọi endpoint nhạy cảm -> bị khóa 403 STEP_UP_REQUIRED và xóa cookie
        expired_res = client.get("/api/permissions/users")
        assert expired_res.status_code == 403
        assert expired_res.json() == {"ok": False, "code": "LOCKED", "locked": True}
        assert "max-age=0" in expired_res.headers.get("set-cookie", "").lower()

        # Mở khóa lại và khóa thủ công bằng POST /api/auth/step-up/lock
        assert client.post("/api/auth/step-up/password", json={"password": "AdminPass123!"}).status_code == 200
        assert client.get("/api/permissions/users").status_code == 200
        lock_res = client.post("/api/auth/step-up/lock")
        assert lock_res.status_code == 200
        assert lock_res.json() == {"ok": True, "locked": True}
        assert client.get("/api/permissions/users").status_code == 403


@pytest.mark.asyncio
async def test_step_up_password_rate_limit(app_container, fake_db, fake_bot):
    """POST /api/auth/step-up/password có rate-limit chống brute-force (429 + Retry-After)."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        for _ in range(5):
            wrong = client.post("/api/auth/step-up/password", json={"password": "WrongPassword!"})
            assert wrong.status_code == 401
        blocked = client.post("/api/auth/step-up/password", json={"password": "AdminPass123!"})
        assert blocked.status_code == 429
        assert int(blocked.headers.get("retry-after", "0")) >= 1


@pytest.mark.asyncio
async def test_step_up_discord_oauth2_strict_identity_match_and_tab_hash(
    app_container, fake_db, fake_bot, monkeypatch
):
    """GET /api/auth/step-up/discord lưu target tab hash trong state đã ký và bắt buộc Discord user_id khớp chính xác."""
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1234567890")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret-value")

    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.permission_service.upsert_permission(
        "1240503090185109536",
        label="Discord Admin",
        is_admin=True,
        can_dashboard=True,
        can_status=True,
    )

    returned_discord_profile = {
        "id": "1240503090185109536",
        "username": "discord_admin",
        "global_name": "Discord Admin",
        "avatar": "",
    }

    async def fake_fetch_discord_user(**_kwargs):
        return dict(returned_discord_profile)

    monkeypatch.setattr(app_container.oauth_service, "fetch_discord_user", fake_fetch_discord_user)
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        # 1) Đăng nhập ban đầu qua Discord OAuth2
        start_login = client.get("/auth/discord", follow_redirects=False)
        assert start_login.status_code == 302
        login_state = parse_qs(urlparse(start_login.headers["location"]).query)["state"][0]
        cb_login = client.get(f"/auth/discord/callback?code=code1&state={login_state}", follow_redirects=False)
        assert cb_login.status_code == 302
        assert cb_login.headers["location"] == "/dashboard"

        # Chưa Step-up -> bị chặn 403
        assert client.get("/api/sessions/search").status_code == 403
        # Tài khoản Discord không thể dùng password step-up
        assert client.post("/api/auth/step-up/password", json={"password": "anything"}).status_code == 400

        # 2) Step-up với Discord ID KHÁC -> bị từ chối, KHÔNG cấp kiwikiwi_stepup, redirect kèm lỗi & giữ #sessions
        returned_discord_profile["id"] = "999999999999999999"
        su_start_bad = client.get("/api/auth/step-up/discord?tab=sessions", follow_redirects=False)
        assert su_start_bad.status_code == 302
        bad_state = parse_qs(urlparse(su_start_bad.headers["location"]).query)["state"][0]
        su_cb_bad = client.get(f"/auth/discord/callback?code=code2&state={bad_state}", follow_redirects=False)
        assert su_cb_bad.status_code == 302
        assert su_cb_bad.headers["location"] == "/dashboard?step_up_error=discord_mismatch#sessions"
        assert client.get("/api/sessions/search").status_code == 403

        # 3) Step-up với đúng Discord ID đang đăng nhập -> cấp kiwikiwi_stepup & redirect về /dashboard?step_up=ok#sessions
        returned_discord_profile["id"] = "1240503090185109536"
        su_start_ok = client.get("/api/auth/step-up/discord?tab=sessions", follow_redirects=False)
        assert su_start_ok.status_code == 302
        ok_state = parse_qs(urlparse(su_start_ok.headers["location"]).query)["state"][0]
        su_cb_ok = client.get(f"/auth/discord/callback?code=code3&state={ok_state}", follow_redirects=False)
        assert su_cb_ok.status_code == 302
        assert su_cb_ok.headers["location"] == "/dashboard?step_up=ok#sessions"
        assert STEP_UP_COOKIE_NAME in su_cb_ok.headers.get("set-cookie", "").lower()

        # Đã mở khóa -> đọc và ghi nhạy cảm đều thành công 200 OK
        assert client.get("/api/sessions/search").status_code == 200
        assert client.get("/api/permissions/discord").status_code == 200
