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
        assert "id=\"login-bot-avatar\"" in login_html.text
        assert "https://cdn.discordapp.com/avatars/999999999999999999/bot.png?size=128" in login_html.text

        # 3) Đăng nhập quyền Operator -> đọc được config nhưng bị chặn 403 ở API quản trị phân quyền & Test Watchdog
        op_login = client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        assert op_login.status_code == 200
        assert client.get("/api/me").json()["user"]["role"] == "operator"
        assert client.post(
            "/api/permissions/discord",
            json={"user_id": "222333444555666777", "label": "Denied", "can_status": True},
        ).status_code == 403
        assert client.post("/api/system/watchdog/test").status_code == 403
        op_dash_html = client.get("/dashboard")
        assert op_dash_html.status_code == 200
        assert "id=\"btn-test-watchdog\"" not in op_dash_html.text

        # 4) Đăng nhập quyền Admin -> render trang Dashboard + cập nhật toàn bộ cấu hình + phân quyền
        adm_login = client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        assert adm_login.status_code == 200

        dash_html = client.get("/dashboard")
        assert dash_html.status_code == 200
        assert "/static/js/dashboard.js" in dash_html.text
        assert "id=\"tab-status\"" in dash_html.text
        assert "id=\"tab-models\"" in dash_html.text
        assert "id=\"st-ram-reserve\"" in dash_html.text
        assert "id=\"btn-test-watchdog\"" in dash_html.text
        assert "id=\"confirm-save-modal\"" in dash_html.text
        assert "https://cdn.discordapp.com/avatars/999999999999999999/bot.png?size=128" in dash_html.text

        status_res = client.get("/api/status?check_gemini=1")
        assert status_res.status_code == 200
        status_json = status_res.json()
        assert status_json["bot"]["online"] is True
        assert status_json["bot"]["display_name"] == "KiwiKiwi"
        assert status_json["bot"]["avatar_url"] == "https://cdn.discordapp.com/avatars/999999999999999999/bot.png?size=128"
        assert status_json["gemini"]["check"]["ok"] is True
        wd_json = status_json["resources"]["memory_watchdog"]
        assert wd_json["reserve_ready"] is True
        assert wd_json["trigger_threshold_mb"] == 464.0
        assert wd_json["recover_threshold_mb"] == 400.0
        assert "Sẵn sàng" in wd_json["status_label"]

        # Admin chạy mô phỏng an toàn quy trình 4 bước MemoryWatchdog qua POST /api/system/watchdog/test
        app_container.user_lookup._user_cache["777888"] = (0.0, {"username": "kiwi_tester"})
        wd_test_res = client.post("/api/system/watchdog/test")
        assert wd_test_res.status_code == 200
        wd_test_json = wd_test_res.json()
        assert wd_test_json["ok"] is True
        assert wd_test_json["simulated"] is True
        assert wd_test_json["event"]["simulated"] is True
        assert wd_test_json["purged_total"] >= 1
        assert wd_test_json["memory_watchdog"]["reserve_ready"] is True
        assert wd_test_json["memory_watchdog"]["emergency_count"] >= 1

        ask_res = client.put(
            "/api/config/ask",
            json={
                "ask": {"user_cooldown": 3.0, "max_concurrent": 4, "default_thinking_level": "medium"},
                "session": {"session_idle_hours": 4.0, "max_history_messages": 30},
                "changes": {
                    "ask.user_cooldown": {"old": 5.0, "new": 3.0},
                    "ask.max_concurrent": {"old": 2, "new": 4},
                },
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

        # 1) Smart Merge mặc định khi điền cả Custom Status + Hoạt động chính (auto_rotate=False)
        merge_presence_res = client.put(
            "/api/config/presence",
            json={
                "status": "online",
                "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
                "activity_type": "listening",
                "activity_text": "m!help",
                "auto_rotate": False,
            },
        )
        assert merge_presence_res.status_code == 200
        merge_json = merge_presence_res.json()
        assert merge_json["applied_live"] is True
        assert merge_json["config"]["presence"] == {
            "status": "online",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "m!help",
            "auto_rotate": False,
        }
        assert len(fake_bot.presence_updates) == 1
        merged_status, merged_activity = fake_bot.presence_updates[-1]
        assert str(merged_status) == "online"
        assert merged_activity is not None
        assert merged_activity.state == "Ngôi nhà nhỏ của KiwiKiwi~ · Listening to m!help"

        # 2) Bật chế độ Luân phiên tự động (mỗi 20s) -> lưu auto_rotate=True và áp dụng bước 1 (Custom Status)
        rotate_presence_res = client.put(
            "/api/config/presence",
            json={
                "status": "idle",
                "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
                "activity_type": "listening",
                "activity_text": "m!help",
                "auto_rotate": True,
            },
        )
        assert rotate_presence_res.status_code == 200
        assert rotate_presence_res.json()["config"]["presence"]["auto_rotate"] is True
        assert getattr(fake_bot, "_presence_rotate_task", None) is not None

        # 3) Kiểm tra cập nhật chỉ Hoạt động chính (tự động dừng vòng lặp luân phiên cũ)
        presence_res = client.put(
            "/api/config/presence",
            json={
                "status": "dnd",
                "custom_status": "",
                "activity_type": "watching",
                "activity_text": "m!help · KiwiKiwi",
                "auto_rotate": False,
            },
        )
        assert presence_res.status_code == 200
        presence_json = presence_res.json()
        assert presence_json["applied_live"] is True
        assert presence_json["config"]["presence"] == {
            "status": "dnd",
            "custom_status": "",
            "activity_type": "watching",
            "activity_text": "m!help · KiwiKiwi",
            "auto_rotate": False,
        }
        assert getattr(fake_bot, "_presence_rotate_task", None) is None
        applied_status, applied_activity = fake_bot.presence_updates[-1]
        assert str(applied_status) == "dnd"
        assert applied_activity is not None and applied_activity.name == "m!help · KiwiKiwi"

        # Từ chối trạng thái không hợp lệ
        assert client.put(
            "/api/config/presence",
            json={"status": "invalid_status", "custom_status": "", "activity_type": "listening", "activity_text": "m!help"},
        ).status_code == 400

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

        # Kiểm tra bộ lọc vai trò server và tra cứu/duyệt toàn bộ phiên chat từ MongoDB Atlas
        await app_container.session_repo.save_turn(
            777888,
            "Kiểm tra từ khóa KiwiKiwi",
            "Phản hồi mẫu cho KiwiKiwi",
            None,
            "gemini-2.5-flash",
        )
        filters_res = client.get("/api/sessions/filters")
        assert filters_res.status_code == 200
        role_names = [r["name"] for r in filters_res.json()["server_roles"]]
        assert "Moderator" in role_names

        # Duyệt toàn bộ không cần nhập ID -> đúng 1 dòng tóm tắt cho 1 session_id + tự bổ sung avatar & username
        browse_res = client.get("/api/sessions/search")
        assert browse_res.status_code == 200
        browse_json = browse_res.json()
        assert browse_json["total"] == 1
        assert browse_json["items"][0]["username"] == "kiwi_tester"
        assert browse_json["items"][0]["preview"] == "Kiểm tra từ khóa KiwiKiwi"
        assert browse_json["items"][0]["message_count"] == 2
        assert browse_json["items"][0]["avatar_url"].startswith("https://cdn.discordapp.com/")

        # Lọc theo username và server_role
        filtered_res = client.get("/api/sessions/search?username=kiwi_tester&server_role=501")
        assert filtered_res.status_code == 200
        assert filtered_res.json()["total"] == 1
        assert all({"chat_id", "preview", "timestamp", "avatar_url"} <= set(row.keys()) for row in filtered_res.json()["items"])

        # Kiểm tra API xem chi tiết phiên chat (mở Modal không chuyển hướng)
        chat_id = browse_json["items"][0]["chat_id"]
        detail_res = client.get(f"/api/sessions/{chat_id}")
        assert detail_res.status_code == 200
        detail_session = detail_res.json()["session"]
        assert detail_session["chat_id"] == chat_id
        assert len(detail_session["messages"]) == 2
        assert client.get("/api/sessions/nonexistent-id").status_code == 404

        # Ghi thêm 1 bản ghi Audit Log từ Discord Admin để kiểm tra tự bổ sung avatar Discord
        await app_container.audit_service.record(
            actor="Kiwi Tester (discord:777888)",
            actor_type="discord",
            action="update_config",
            section="ask",
            summary="Chỉnh cooldown từ Discord",
            changes=[{"field": "ask.user_cooldown", "before": 5.0, "after": 3.0}],
        )

        # Kiểm tra Audit Log phân trang phía server (mặc định 5 mục/trang, page 1 & page 2, alias /api/audit)
        audit_res = client.get("/api/audit-logs")
        assert audit_res.status_code == 200
        audit_json = audit_res.json()
        assert audit_json["page"] == 1
        assert audit_json["limit"] == 5
        assert len(audit_json["items"]) == 5
        assert audit_json["total_items"] >= 6
        assert audit_json["total_pages"] >= 2

        discord_audit = next(item for item in audit_json["items"] if item.get("actor_discord_id") == "777888")
        assert discord_audit["actor_avatar_url"].startswith("https://cdn.discordapp.com/")
        assert discord_audit["changes"] == [{"field": "ask.user_cooldown", "before": 5.0, "after": 3.0}]

        # Kiểm tra trang 2 và alias /api/audit + tự kẹp limit > 20 & chấp nhận category rỗng/all (không gây lỗi 422)
        audit_p2 = client.get("/api/audit?page=2&limit=5")
        assert audit_p2.status_code == 200
        p2_json = audit_p2.json()
        assert p2_json["page"] == 2
        assert p2_json["limit"] == 5
        assert len(p2_json["items"]) >= 1
        assert p2_json["items"][0] != audit_json["items"][0]

        clamped_res = client.get("/api/audit-logs?limit=100&page=&category=")
        assert clamped_res.status_code == 200
        assert clamped_res.json()["limit"] == 20
        assert clamped_res.json()["page"] == 1

        all_cat_res = client.get("/api/audit?page=1&limit=5&category=all")
        assert all_cat_res.status_code == 200
        assert all_cat_res.json()["total_items"] >= 6

        # Xác minh bản ghi MongoDB chứa ObjectId và datetime thật được tuần tự hóa JSON an toàn (không lỗi 500)
        from datetime import datetime, timezone
        from bson import ObjectId

        raw_oid = ObjectId()
        await fake_db["audit_logs"].insert_one({
            "_id": raw_oid,
            "at": datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            "actor": "atlas-manual",
            "actor_type": "system",
            "action": "manual_edit",
            "section": "tunnel",
            "summary": "Kiểm tra tuần tự hóa ObjectId và datetime",
            "changes": [],
        })
        bson_res = client.get("/api/audit?page=1&limit=5&category=tunnel")
        assert bson_res.status_code == 200
        bson_items = bson_res.json()["items"]
        assert bson_items[0]["_id"] == str(raw_oid)
        assert bson_items[0]["at"].startswith("2099-01-01T12:00:00")


@pytest.mark.asyncio
async def test_login_page_uses_cached_bot_avatar_from_mongodb_and_official_clyde_svg(
    app_container, fake_db, monkeypatch
):
    """Trang /login vẫn hiển thị avatar bot từ `bot_metrics` khi bot chưa ready và dùng logo Discord Clyde chính thức."""
    monkeypatch.setenv("DISCORD_CLIENT_ID", "1234567890")
    monkeypatch.setenv("DISCORD_CLIENT_SECRET", "super-secret-oauth")

    app_container.usage_stats.attach_db(fake_db)
    await app_container.usage_stats.save_bot_branding(
        "https://cdn.discordapp.com/avatars/999999999999999999/persisted.png?size=128",
        "KiwiKiwi",
    )
    # Xóa cache RAM để buộc trang /login đọc lại từ MongoDB `bot_metrics` (`runtime_snapshot`)
    app_container.usage_stats.bot_avatar_url = ""
    stored = await fake_db["bot_metrics"].find_one({"_id": "runtime_snapshot"})
    assert stored["bot_avatar_url"] == "https://cdn.discordapp.com/avatars/999999999999999999/persisted.png?size=128"
    assert stored["bot_name"] == "KiwiKiwi"

    # Mô phỏng giai đoạn khởi động khi bot chưa kết nối Discord (get_bot -> None)
    web_app = create_dashboard_app(app_container, get_bot=lambda: None)
    with TestClient(web_app) as client:
        res = client.get("/login")
        assert res.status_code == 200
        assert "https://cdn.discordapp.com/avatars/999999999999999999/persisted.png?size=128" in res.text
        assert 'id="login-bot-avatar"' in res.text
        assert 'viewBox="0 0 127.14 96.36"' in res.text
        assert "Đăng nhập với Discord" in res.text
