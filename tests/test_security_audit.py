"""Bộ kiểm thử hồi quy bảo mật toàn diện (Auth/RBAC, Zero-Secret/Zero-Hash, Tunnel Spoofing, Subprocess, NoSQL, Rate Limit, XSS/CSP)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.cloudflare_tunnel import CloudflareTunnelService
from app.core.errors import UserError
from app.core.security import (
    STATIC_SECRET_PLACEHOLDER,
    is_trusted_proxy_ip,
    redact_secrets,
    sanitize_config_for_response,
    validate_cloudflare_tunnel_token,
)
from app.features.chat.attachments import read_attachment
from app.web.app import create_dashboard_app


@pytest.mark.asyncio
async def test_every_api_endpoint_rejects_unauthenticated_and_disables_openapi(app_container, fake_db, fake_bot):
    """Mọi endpoint `/api/*` đều bắt buộc xác thực (401) và `/openapi.json`, `/docs`, `/redoc` bị tắt hoàn toàn."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404

        protected_calls = [
            ("GET", "/api/me", None),
            ("GET", "/api/status", None),
            ("POST", "/api/system/watchdog/test", None),
            ("GET", "/api/config", None),
            ("PUT", "/api/config/ask", {"ask": {}, "session": {}}),
            ("PUT", "/api/config/prompt", {"system_prompt": "x", "ocr_instruction": "y"}),
            ("PUT", "/api/config/presence", {"status": "online"}),
            ("PUT", "/api/config/tunnel", {"cf_tunnel_enabled": False, "cf_tunnel_mode": "named"}),
            ("PUT", "/api/config/models", {}),
            ("POST", "/api/models/refresh", None),
            ("POST", "/api/models/clear-cooldowns", None),
            ("GET", "/api/permissions/discord", None),
            ("POST", "/api/permissions/discord", {"user_id": "123456789012345678"}),
            ("DELETE", "/api/permissions/discord/123456789012345678", None),
            ("GET", "/api/permissions/users", None),
            ("POST", "/api/permissions/users", {"username": "abc", "password": "Password123!"}),
            ("PUT", "/api/permissions/users/admin/password", {"password": "Password123!"}),
            ("DELETE", "/api/permissions/users/admin", None),
            ("GET", "/api/sessions/filters", None),
            ("GET", "/api/sessions/search", None),
            ("GET", "/api/sessions/some-chat-id", None),
            ("GET", "/api/audit-logs", None),
            ("GET", "/api/audit", None),
        ]
        for method, path, body in protected_calls:
            res = client.request(method, path, json=body)
            assert res.status_code == 401, f"{method} {path} returned {res.status_code} instead of 401"


@pytest.mark.asyncio
async def test_zero_secret_and_zero_hash_exposure_across_all_api_responses(app_container, fake_db, fake_bot):
    """Đảm bảo KHÔNG BAO GIỜ lộ `cf_tunnel_token`, tiền tố token, `password_hash`, `salt` hay `token_hash` ở bất kỳ API nào."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.model_registry.refresh()
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await app_container.auth_service.create_user("operator1", "OperPass123!", role="operator")

    secret_token = "eyJhIjoiU2VjcmV0VHVubmVsVG9rZW5WYWx1ZTEyMzQ1Njc4OTAifQ=="
    token_prefix = secret_token[:4]

    # Lưu sẵn token và mô phỏng trường hash/secret lạ trong MongoDB `bot_config` & bản ghi Audit Log lịch sử
    cfg_doc = await fake_db["bot_config"].find_one({"_id": "global"})
    assert cfg_doc is not None
    cfg_doc["tunnel"]["cf_tunnel_token"] = secret_token
    cfg_doc["password_hash"] = "scrypt$16384$8$1$deadbeef$cafebabe"
    cfg_doc["token_hash"] = "sha256-secret-digest"
    cfg_doc["salt"] = "random-salt-value"
    await fake_db["bot_config"].replace_one({"_id": "global"}, cfg_doc, upsert=True)
    app_container.config_repo.apply_external_config(cfg_doc)

    # Chèn bản ghi Audit Log lịch sử chứa tiền tố bí mật cũ để kiểm chứng read-time scrubbing
    await fake_db["audit_logs"].insert_one({
        "at": "2026-11-25T10:00:00+00:00",
        "actor": "legacy-admin",
        "actor_type": "password",
        "action": "update_config",
        "section": "tunnel",
        "summary": "Legacy tunnel update",
        "password_hash": "scrypt$16384$8$1$abcd$1234",
        "changes": [
            {"field": "tunnel.cf_tunnel_token", "before": "", "after": f"{token_prefix}•••••"},
        ],
    })

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        # Operator gọi PUT /api/config/models (trước đây từng lộ plaintext cf_tunnel_token!)
        client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        models_res = client.put(
            "/api/config/models",
            json={
                "default_text_model": "gemini-3.5-flash",
                "auto_model_chain": ["gemini-3.5-flash"],
                "auto_last_model": "gemini-4-hydrogen",
                "overrides": {"$set": {"tag": "bad"}, "gemini-3.5-flash": {"tag": "🆓"}},
            },
        )
        assert models_res.status_code == 200
        models_body = models_res.text
        assert secret_token not in models_body
        assert token_prefix not in models_body
        assert "cf_tunnel_token" not in models_res.json()["config"]["tunnel"]
        assert models_res.json()["config"]["tunnel"]["has_cf_tunnel_token"] is True
        assert "password_hash" not in models_body
        assert "token_hash" not in models_body
        assert "salt" not in models_body
        assert "$set" not in models_res.json()["config"]["models"]["overrides"]

        # Kiểm tra toàn bộ các endpoint đọc/ghi khác
        for method, path, payload in [
            ("GET", "/api/config", None),
            ("PUT", "/api/config/ask", {"ask": {"user_cooldown": 3.0}, "session": {}}),
            ("PUT", "/api/config/prompt", {"system_prompt": "Xin chào", "ocr_instruction": "OCR"}),
            ("PUT", "/api/config/presence", {"status": "online", "activity_type": "none", "activity_text": ""}),
            ("GET", "/api/me", None),
            ("GET", "/api/permissions/users", None),
            ("GET", "/api/audit-logs?limit=20", None),
            ("GET", "/api/status", None),
        ]:
            res = client.request(method, path, json=payload)
            assert res.status_code == 200
            raw = res.text
            assert secret_token not in raw, f"Secret token leaked in {method} {path}"
            assert "password_hash" not in raw, f"password_hash leaked in {method} {path}"
            assert "token_hash" not in raw, f"token_hash leaked in {method} {path}"
            assert "session_version" not in raw, f"session_version leaked in {method} {path}"
            assert "scrypt$" not in raw, f"scrypt hash leaked in {method} {path}"

        # Bản ghi Audit Log lịch sử chứa tiền tố `eyJh•••••` phải bị che thành `••••••••••••` khi đọc ra
        audit_items = client.get("/api/audit-logs?section=tunnel&limit=20").json()["items"]
        assert token_prefix not in json.dumps(audit_items)
        legacy = next(item for item in audit_items if item.get("actor") == "legacy-admin")
        assert legacy["changes"][0]["after"] == STATIC_SECRET_PLACEHOLDER


@pytest.mark.asyncio
async def test_session_revocation_on_password_change_and_user_deletion(app_container, fake_db, fake_bot):
    """Đổi mật khẩu hoặc xóa tài khoản phải thu hồi ngay cookie phiên đang hoạt động (chống phiên ma 7 ngày)."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await app_container.auth_service.create_user("victim", "OldPass123!", role="operator")

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as victim_client, TestClient(web_app) as admin_client:
        login_res = victim_client.post("/auth/login", json={"username": "victim", "password": "OldPass123!"})
        assert login_res.status_code == 200
        set_cookie = login_res.headers.get("set-cookie", "").lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie
        assert victim_client.get("/api/me").status_code == 200

        # Admin đổi mật khẩu của victim -> cookie cũ lập tức bị từ chối 401
        admin_client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        assert admin_client.put(
            "/api/permissions/users/victim/password",
            json={"password": "NewPass456!"},
        ).status_code == 200
        assert victim_client.get("/api/me").status_code == 401

        # Victim đăng nhập bằng mật khẩu mới -> hợp lệ, sau đó Admin xóa tài khoản -> cookie lập tức bị 401
        assert victim_client.post("/auth/login", json={"username": "victim", "password": "NewPass456!"}).status_code == 200
        assert victim_client.get("/api/me").status_code == 200
        assert admin_client.delete("/api/permissions/users/victim").status_code == 200
        assert victim_client.get("/api/me").status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limiting_payload_limit_and_post_only_logout(app_container, fake_db, fake_bot):
    """Kiểm tra chống brute-force /auth/login (429), giới hạn thân request >256KB (413) và logout chỉ nhận POST."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        # 5 lần đăng nhập sai -> 401, lần thứ 6 bị chặn 429 kèm Retry-After
        for _ in range(5):
            res = client.post("/auth/login", json={"username": "admin", "password": "wrong-password"})
            assert res.status_code == 401
        blocked = client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        assert blocked.status_code == 429
        assert int(blocked.headers.get("retry-after", "0")) >= 1

        # Payload vượt 256KB bị từ chối 413 Payload Too Large
        oversized = {"system_prompt": "A" * (270 * 1024), "ocr_instruction": "B"}
        assert client.put("/api/config/prompt", json=oversized).status_code == 413

        # GET /auth/logout bị chặn (405) để chống Logout CSRF; POST /auth/logout hoạt động bình thường (303)
        assert client.get("/auth/logout", follow_redirects=False).status_code == 405
        post_logout = client.post("/auth/logout", follow_redirects=False)
        assert post_logout.status_code == 303
        assert post_logout.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_tunnel_only_inbound_blocks_forged_headers_from_external_ip(app_container, fake_db, fake_bot):
    """Khi `tunnel_only_inbound=True`, client từ IP ngoài (không phải loopback/proxy tin cậy) giả mạo `Cf-Ray` & `CF-Connecting-IP` vẫn bị chặn 403."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    cfg = await app_container.config_service.get_config(force=True)
    cfg["tunnel"]["tunnel_only_inbound"] = True
    app_container.config_repo.apply_external_config(cfg)

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    # Mô phỏng request đến từ IP công cộng bên ngoài container (203.0.113.99) kèm header Cloudflare giả mạo
    external_transport = httpx.ASGITransport(app=web_app, client=("203.0.113.99", 54321))
    async with httpx.AsyncClient(transport=external_transport, base_url="http://example.com") as ext_client:
        spoofed = await ext_client.get(
            "/login",
            headers={"Cf-Ray": "89ab123456-SIN", "CF-Connecting-IP": "198.51.100.5"},
        )
        assert spoofed.status_code == 403
        # /health vẫn mở cho health probe của nền tảng container
        assert (await ext_client.get("/health")).status_code == 200

    # Mô phỏng request đi qua `cloudflared` cục bộ (127.0.0.1) kèm header Cloudflare hợp lệ -> cho phép 200
    loopback_transport = httpx.ASGITransport(app=web_app, client=("127.0.0.1", 54321))
    async with httpx.AsyncClient(transport=loopback_transport, base_url="http://127.0.0.1:10000") as local_client:
        legit = await local_client.get(
            "/login",
            headers={
                "Cf-Ray": "89ab123456-SIN",
                "CF-Connecting-IP": "198.51.100.5",
                "X-Forwarded-For": "198.51.100.5",
                "X-Forwarded-Proto": "https",
            },
        )
        assert legit.status_code == 200
        # Loopback nhưng KHÔNG có header CF -> vẫn bị chặn 403
        no_cf = await local_client.get("/login")
        assert no_cf.status_code == 403

        # Bật động `proxy_headers=True` trong cấu hình tunnel: vẫn cho phép qua Tunnel (không phá vỡ `tunnel_only_inbound`)
        cfg["tunnel"]["proxy_headers"] = True
        app_container.config_repo.apply_external_config(cfg)
        with_proxy_headers = await local_client.get(
            "/login",
            headers={
                "Cf-Ray": "89ab123456-SIN",
                "CF-Connecting-IP": "198.51.100.5",
                "X-Forwarded-For": "198.51.100.5",
                "X-Forwarded-Proto": "https",
            },
        )
        assert with_proxy_headers.status_code == 200
        assert "strict-transport-security" in with_proxy_headers.headers

    assert is_trusted_proxy_ip("127.0.0.1") is True
    assert is_trusted_proxy_ip("::1") is True
    assert is_trusted_proxy_ip("203.0.113.99") is False


@pytest.mark.asyncio
async def test_cloudflared_subprocess_rejects_flag_injection_and_isolates_secrets(monkeypatch):
    """Xác minh token độc hại (`--config=...`, `; id`) bị từ chối và tiến trình con `cloudflared` không kế thừa bí mật của bot."""
    monkeypatch.setenv("DISCORD_TOKEN", "bot-secret-should-not-leak")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySecretKeyShouldNotLeak1234567890")
    monkeypatch.setenv("MONGODB_URI", "mongodb+srv://admin:superpass@cluster.mongodb.net/db")
    monkeypatch.setenv("SESSION_SECRET", "session-signing-secret-should-not-leak")

    for bad_token in [
        "--config=/etc/passwd",
        "-token",
        "eyJvalid;id",
        "eyJvalid\n--url=http://evil",
        "eyJvalid $(whoami)",
        "short",
    ]:
        with pytest.raises(ValueError):
            validate_cloudflare_tunnel_token(bad_token)

    captured_env: dict[str, str] = {}
    captured_cmd: tuple[str, ...] = ()

    async def fake_spawn(*cmd, **kwargs):
        nonlocal captured_cmd, captured_env
        captured_cmd = cmd
        captured_env = dict(kwargs.get("env") or {})
        return SimpleNamespace(pid=9999, returncode=None, stderr=None, terminate=lambda: None, wait=lambda: None)

    svc = CloudflareTunnelService(local_port=10000, spawn_subprocess=fake_spawn)
    bad_status = await svc.apply_config({
        "cf_tunnel_enabled": True,
        "cf_tunnel_mode": "named",
        "cf_tunnel_token": "--config=/etc/passwd",
    })
    assert bad_status["running"] is False
    assert captured_cmd == ()

    good_token = "eyJhIjoiVGVzdFR1bm5lbFRva2VuMTIzNDU2Nzg5MCJ9"
    good_status = await svc.apply_config({
        "cf_tunnel_enabled": True,
        "cf_tunnel_mode": "named",
        "cf_tunnel_token": good_token,
    })
    assert good_status["running"] is True
    assert good_token not in captured_cmd
    assert captured_env["TUNNEL_TOKEN"] == good_token
    for secret_env in ("DISCORD_TOKEN", "GEMINI_API_KEY", "MONGODB_URI", "SESSION_SECRET"):
        assert secret_env not in captured_env


@pytest.mark.asyncio
async def test_oauth2_csrf_state_and_security_headers_and_redaction(app_container, fake_db, fake_bot, monkeypatch):
    """Kiểm tra chống CSRF OAuth2, tiêu đề bảo mật HTTP, redaction lỗi và chặn file SVG."""
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1234567890")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "oauth-secret-value")
    await app_container.sync_coordinator.attach_db(fake_db)
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        # Callback thiếu hoặc sai state -> redirect về /login kèm lỗi và xóa cookie state
        cb_res = client.get("/auth/discord/callback?code=abc&state=forged-state", follow_redirects=False)
        assert cb_res.status_code == 302
        assert cb_res.headers["location"].startswith("/login?error=")

        # Kiểm tra bộ HTTP Security Headers chuẩn
        login_res = client.get("/login")
        assert login_res.headers.get("x-content-type-options") == "nosniff"
        assert login_res.headers.get("x-frame-options") == "DENY"
        assert login_res.headers.get("referrer-policy") == "no-referrer"
        assert "frame-ancestors 'none'" in login_res.headers.get("content-security-policy", "")

        # Kiểm tra NoSQL filter an toàn trên Audit Log & Sessions
        await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
        client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        nosql_audit = client.get("/api/audit-logs?section=$ne")
        assert nosql_audit.status_code == 200
        assert nosql_audit.json()["items"] == []
        assert client.get("/api/sessions/$ne").status_code == 404
        assert client.get("/api/sessions/search?q=((a+)+)$").status_code == 200

    # Kiểm tra redact_secrets che sạch mọi chuỗi bí mật và hash
    dirty = (
        "Failed connecting mongodb+srv://user:SuperSecretPass@cluster0.mongodb.net/db "
        "with key=AIzaSyA123456789012345678901234567890 and scrypt$16384$8$1$aabb$ccdd"
    )
    redacted = redact_secrets(dirty)
    assert "SuperSecretPass" not in redacted
    assert "AIzaSyA1234567890" not in redacted
    assert "aabb$ccdd" not in redacted

    # Kiểm tra sanitize_config_for_response luôn trả cờ boolean và không bao giờ trả khóa nhạy cảm
    safe_cfg = sanitize_config_for_response({
        "tunnel": {"cf_tunnel_token": "eyJ-top-secret-1234", "cf_tunnel_mode": "named"},
        "nested": {"api_key": "AIza-leak", "password_hash": "scrypt$1$1$1$aa$bb"},
    })
    assert safe_cfg["tunnel"]["has_cf_tunnel_token"] is True
    assert "cf_tunnel_token" not in safe_cfg["tunnel"]
    assert "api_key" not in safe_cfg["nested"]
    assert "password_hash" not in safe_cfg["nested"]

    # Kiểm tra read_attachment chặn SVG
    svg_attachment = SimpleNamespace(
        size=100,
        content_type="image/svg+xml",
        filename="xss.svg",
        read=lambda: b"<svg onload=alert(1)>",
    )
    with pytest.raises(UserError):
        await read_attachment(svg_attachment, max_file_mb=5.0)


@pytest.mark.asyncio
async def test_password_pepper_hmac_prehash_and_seamless_legacy_migration(app_container, fake_db, fake_bot, monkeypatch):
    """Kiểm tra tiền băm HMAC-SHA256 với PASSWORD_PEPPER, nâng cấp mượt hash cũ không khóa admin và không lộ bí mật."""
    import hashlib
    import hmac

    from app.core.security import (
        apply_password_pepper,
        hash_password,
        verify_password_with_migration,
    )

    pepper_secret = "super-secret-pepper-key-2026-not-in-db"
    long_password = "P" * 96 + "!Admin2026"  # > 72 bytes để kiểm chứng tiền băm HMAC-SHA256 luôn cố định 64 ký tự hex

    # 1) Công thức tiền băm HMAC-SHA256 chuẩn
    expected_prehash = hmac.new(
        pepper_secret.encode("utf-8"),
        long_password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    peppered_material = apply_password_pepper(long_password, pepper=pepper_secret)
    assert peppered_material == expected_prehash
    assert len(peppered_material) == 64

    # 2) Tạo tài khoản Admin cũ TRƯỚC khi bật PASSWORD_PEPPER (legacy unpeppered hash trong MongoDB)
    monkeypatch.delenv("PASSWORD_PEPPER", raising=False)
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", long_password, role="admin")

    legacy_doc = await fake_db["dashboard_users"].find_one({"username": "admin"})
    assert legacy_doc is not None
    legacy_hash = legacy_doc["password_hash"]
    # Xác nhận hash cũ là unpeppered
    assert verify_password_with_migration(long_password, legacy_hash, pepper="") == (True, False)
    assert verify_password_with_migration(long_password, legacy_hash, pepper=pepper_secret) == (True, True)

    # 3) Bật PASSWORD_PEPPER trong môi trường -> đăng nhập lần đầu KHÔNG bị khóa và tự nâng cấp hash trong MongoDB
    monkeypatch.setenv("PASSWORD_PEPPER", pepper_secret)
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        # Sai mật khẩu vẫn bị từ chối 401 và không ghi đè hash
        bad_res = client.post("/auth/login", json={"username": "admin", "password": "WrongPassword123!"})
        assert bad_res.status_code == 401
        still_legacy = await fake_db["dashboard_users"].find_one({"username": "admin"})
        assert still_legacy["password_hash"] == legacy_hash

        # Đăng nhập đúng mật khẩu -> 200 OK và hash trong MongoDB được tự động nâng cấp sang bản có pepper
        login_res = client.post("/auth/login", json={"username": "admin", "password": long_password})
        assert login_res.status_code == 200
        assert pepper_secret not in login_res.text
        assert "password_hash" not in login_res.text

        upgraded_doc = await fake_db["dashboard_users"].find_one({"username": "admin"})
        assert upgraded_doc is not None
        upgraded_hash = upgraded_doc["password_hash"]
        assert upgraded_hash != legacy_hash
        assert pepper_secret not in str(upgraded_doc)

        # Sau khi nâng cấp: xác thực với pepper khớp trực tiếp (needs_migration=False), xác thực không có pepper phải thất bại
        assert verify_password_with_migration(long_password, upgraded_hash, pepper=pepper_secret) == (True, False)
        assert verify_password_with_migration(long_password, upgraded_hash, pepper="") == (False, False)

        # Đăng nhập lần 2 tiếp tục thành công và không cần migration nữa
        login_res_2 = client.post("/auth/login", json={"username": "admin", "password": long_password})
        assert login_res_2.status_code == 200
        after_second_doc = await fake_db["dashboard_users"].find_one({"username": "admin"})
        assert after_second_doc["password_hash"] == upgraded_hash

        # Tạo tài khoản mới hoặc đổi mật khẩu khi PASSWORD_PEPPER đang bật -> lưu trực tiếp hash có pepper
        assert client.post(
            "/api/permissions/users",
            json={"username": "newop", "password": "NewOpPassword123!", "role": "operator"},
        ).status_code == 200
        newop_doc = await fake_db["dashboard_users"].find_one({"username": "newop"})
        assert verify_password_with_migration("NewOpPassword123!", newop_doc["password_hash"], pepper=pepper_secret) == (True, False)
        assert verify_password_with_migration("NewOpPassword123!", newop_doc["password_hash"], pepper="") == (False, False)

        # Kiểm tra Zero Exposure trên /api/me, /api/permissions/users và /api/audit-logs
        for path in ("/api/me", "/api/permissions/users", "/api/audit-logs?limit=20"):
            res = client.get(path)
            assert res.status_code == 200
            assert pepper_secret not in res.text
            assert "password_hash" not in res.text
            assert upgraded_hash not in res.text

    # Kiểm tra hash_password với pepper khác nhau cho ra kết quả không tương thích
    h_with_pepper = hash_password("SamePass123!", pepper=pepper_secret)
    assert verify_password_with_migration("SamePass123!", h_with_pepper, pepper="wrong-pepper") == (False, False)

