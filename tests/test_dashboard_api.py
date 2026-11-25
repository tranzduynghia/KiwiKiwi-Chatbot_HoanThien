"""Kiểm thử tích hợp toàn bộ API và giao diện HTML/tĩnh của FastAPI Web Dashboard."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.web.app import create_dashboard_app


@pytest.mark.asyncio
async def test_dashboard_end_to_end_routes_and_rbac(app_container, fake_db, fake_bot):
    """Xác minh /health, /login, /static, đăng nhập, phân quyền Admin/Operator và các API cấu hình."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.model_registry.refresh()

    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await app_container.auth_service.create_user("operator1", "OperPass123!", role="operator")

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        # 1) Health check & tài nguyên tĩnh công khai
        health_res = client.get("/health")
        assert health_res.status_code == 200
        assert health_res.json()["status"] == "ok"
        assert health_res.json()["discord_ready"] is True

        css_res = client.get("/static/css/dashboard.css")
        assert css_res.status_code == 200
        js_res = client.get("/static/js/dashboard.js")
        assert js_res.status_code == 200

        # 2) Chưa đăng nhập -> bị chuyển hướng về /login và 401 với API
        assert client.get("/dashboard", follow_redirects=False).status_code == 302
        assert client.get("/api/config").status_code == 401
        login_html = client.get("/login")
        assert login_html.status_code == 200
        assert "/static/js/features/auth/login.js" in login_html.text

        # 3) Đăng nhập quyền Operator -> đọc được config nhưng bị chặn 403 ở API quản trị phân quyền
        op_login = client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        assert op_login.status_code == 200
        assert client.get("/api/me").json()["user"]["role"] == "operator"
        assert client.post(
            "/api/permissions/discord",
            json={"user_id": "222333444555666777", "label": "Denied", "can_status": True},
        ).status_code == 403

        # 4) Đăng nhập quyền Admin -> render trang Dashboard + cập nhật toàn bộ cấu hình + phân quyền
        adm_login = client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        assert adm_login.status_code == 200

        dash_html = client.get("/dashboard")
        assert dash_html.status_code == 200
        assert "/static/js/dashboard.js" in dash_html.text
        assert "id=\"tab-status\"" in dash_html.text
        assert "id=\"tab-models\"" in dash_html.text

        status_res = client.get("/api/status?check_gemini=1")
        assert status_res.status_code == 200
        status_json = status_res.json()
        assert status_json["bot"]["online"] is True
        assert status_json["gemini"]["check"]["ok"] is True

        ask_res = client.put(
            "/api/config/ask",
            json={
                "ask": {"user_cooldown": 3.0, "max_concurrent": 4, "default_thinking_level": "medium"},
                "session": {"session_idle_hours": 4.0, "max_history_messages": 30},
            },
        )
        assert ask_res.status_code == 200
        assert ask_res.json()["config"]["ask"]["max_concurrent"] == 4

        prompt_res = client.put(
            "/api/config/prompt",
            json={
                "system_prompt": "Bạn là trợ lý KiwiKiwi phiên bản mới.",
                "ocr_instruction": "Trích xuất văn bản chính xác.",
                "append_current_date": False,
            },
        )
        assert prompt_res.status_code == 200
        assert prompt_res.json()["config"]["append_current_date"] is False

        models_res = client.put(
            "/api/config/models",
            json={
                "default_text_model": "gemini-3.5-flash",
                "auto_model_chain": ["gemini-3.5-flash", "gemini-2.5-flash"],
                "auto_last_model": "gemini-4-hydrogen",
                "hide_pro_models": True,
                "extra_models": ["gemini-4-hydrogen"],
                "hidden_models": ["gemini-2.5-pro"],
                "model_refresh_hours": 4.0,
                "overrides": {"gemini-3.5-flash": {"display_name": "Flash 3.5", "tag": "🆓", "is_free": True}},
            },
        )
        assert models_res.status_code == 200
        assert models_res.json()["models_runtime"]["auto_chain"][0] == "gemini-3.5-flash"

        assert client.post("/api/models/refresh").status_code == 200
        assert client.post("/api/models/clear-cooldowns").status_code == 200

        # Quản lý Discord permissions & Dashboard users
        add_perm = client.post(
            "/api/permissions/discord",
            json={"user_id": "222333444555666777", "label": "Mod", "can_status": True},
        )
        assert add_perm.status_code == 200
        assert client.delete("/api/permissions/discord/222333444555666777").status_code == 200

        add_user = client.post(
            "/api/permissions/users",
            json={"username": "helper1", "password": "HelperPass123!", "role": "operator"},
        )
        assert add_user.status_code == 200
        assert client.put("/api/permissions/users/helper1/password", json={"password": "NewPass456!"}).status_code == 200
        assert client.delete("/api/permissions/users/helper1").status_code == 200

        # Kiểm tra Audit Log đã ghi nhận toàn bộ thay đổi
        audit_res = client.get("/api/audit-logs")
        assert audit_res.status_code == 200
        assert len(audit_res.json()["items"]) >= 5
