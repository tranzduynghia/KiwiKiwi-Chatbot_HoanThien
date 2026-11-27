"""Kiểm thử 3 tính năng: Mẫu ToS Discord động, Thu hồi chấp thuận ToS (xóa cache RAM), và Danh sách đen Discord ID."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.bot.tos_gate import build_tos_prompt_message
from app.core.settings import AppSettings
from app.main import build_container
from app.web.app import create_dashboard_app


def _make_fake_interaction(user_id: int, command_name: str = "ask"):
    sent: list[dict] = []

    async def _send_message(content=None, *, view=None, ephemeral=False):
        sent.append({"content": content, "view": view, "ephemeral": ephemeral})

    response = SimpleNamespace(
        is_done=lambda: False,
        send_message=_send_message,
    )
    return (
        SimpleNamespace(
            user=SimpleNamespace(id=user_id, display_name=f"User{user_id}", name=f"user{user_id}"),
            command=SimpleNamespace(name=command_name, qualified_name=command_name),
            data={"name": command_name},
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
            channel_id=999001,
        ),
        sent,
    )


def _make_fake_message(user_id: int):
    replied: list[dict] = []

    async def _reply(content=None, **kwargs):
        replied.append({"content": content, **kwargs})

    return (
        SimpleNamespace(
            author=SimpleNamespace(id=user_id, bot=False, display_name=f"User{user_id}", name=f"user{user_id}"),
            guild=None,
            mentions=[],
            reference=None,
            channel=SimpleNamespace(id=888001),
            reply=_reply,
        ),
        replied,
    )


@pytest.mark.asyncio
async def test_dynamic_discord_tos_prompt_rendering_and_save(fake_db, fake_gemini, fake_bot):
    """Lưu mẫu tin nhắn Discord ToS qua API, render động `{version}`, `{previous_version}`, `{base_url}` & nhãn nút mới không làm đổi `tos_updated_at`."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-discord-tos-prompt-32chars",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)
    await container.legal_doc_repo.attach_db(fake_db)
    await container.tos_acceptance_repo.attach_db(fake_db)
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        before_cfg = await fake_db["bot_config"].find_one({"_id": "global"})
        before_tos_ts = (before_cfg.get("legal") or {}).get("tos_updated_at")

        # Cập nhật mẫu tin nhắn Discord & nhãn nút
        resp = client.put(
            "/api/legal/discord-prompt",
            json={
                "discord_tos_welcome_template": "Chào mừng tới bản {version}! Xem tại {base_url}/terms {unknown_brace}",
                "discord_tos_update_template": "Nâng từ {previous_version} lên {version} ({base_url}/policies)",
                "discord_tos_button_label": "Chấp nhận ngay",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["discord_prompt"]["discord_tos_button_label"] == "Chấp nhận ngay"

        # Kiểm tra không tự sửa tos_updated_at
        after_cfg = await fake_db["bot_config"].find_one({"_id": "global"})
        assert (after_cfg.get("legal") or {}).get("tos_updated_at") == before_tos_ts

        # Kiểm tra TosConsentGate render đúng mẫu lần đầu + nhãn nút tùy chỉnh + giữ nguyên `{unknown_brace}` an toàn
        interaction, sent = _make_fake_interaction(user_id=200000000000000001)
        allowed = await container.tos_gate.check_interaction(interaction)
        assert allowed is False
        assert len(sent) == 1
        assert sent[0]["ephemeral"] is True
        assert "Chào mừng tới bản v1.0!" in sent[0]["content"]
        assert "/terms" in sent[0]["content"]
        assert "{unknown_brace}" in sent[0]["content"]
        assert sent[0]["view"].accept_button.label == "Chấp nhận ngay"
        sent[0]["view"].stop()

        # Kiểm tra mẫu cập nhật phiên bản thay thế đúng `{previous_version}` & `{version}`
        rendered_update = build_tos_prompt_message(
            active_version="v2.0",
            is_update=True,
            accepted_version="v1.0",
            welcome_template=body["discord_prompt"]["discord_tos_welcome_template"],
            update_template=body["discord_prompt"]["discord_tos_update_template"],
            base_url="https://kiwikiwi.dpdns.org",
        )
        assert rendered_update == "Nâng từ v1.0 lên v2.0 (https://kiwikiwi.dpdns.org/policies)"


@pytest.mark.asyncio
async def test_tos_consent_revocation_evicts_ram_cache_and_logs_audit(fake_db, fake_gemini, fake_bot):
    """Thu hồi chấp thuận ToS xóa bản ghi MongoDB, xóa ngay RAM cache O(1), ghi `TOS_CONSENT_REVOKED` và chặn ngay lượt `/ask` kế tiếp."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-tos-revoke-32chars-long",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)
    await container.legal_doc_repo.attach_db(fake_db)
    await container.tos_acceptance_repo.attach_db(fake_db)
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")

    target_uid = 300000000000000001
    await container.legal_service.accept_current_tos(user_id=target_uid, username="TesterOne", source="discord")
    assert container.legal_service.has_accepted_current_tos_fast(target_uid) is True

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        # Kiểm tra CSRF Origin sai bị chặn 403
        bad_csrf = client.post(
            f"/api/legal/acceptances/{target_uid}/revoke",
            headers={"Origin": "https://evil.example.com"},
        )
        assert bad_csrf.status_code == 403

        # Thu hồi hợp lệ
        revoke_resp = client.post(f"/api/legal/acceptances/{target_uid}/revoke")
        assert revoke_resp.status_code == 200
        payload = revoke_resp.json()
        assert payload["ok"] is True
        assert payload["revoked"] is True
        assert payload["previous_version"] == "v1.0"

    # RAM cache phải bị xóa tức thì và lượt `/ask` kế tiếp phải bị chặn yêu cầu đồng ý lại
    assert container.legal_service.has_accepted_current_tos_fast(target_uid) is False
    assert await fake_db["tos_acceptances"].find_one({"discord_user_id": str(target_uid)}) is None

    interaction, sent = _make_fake_interaction(user_id=target_uid)
    assert await container.tos_gate.check_interaction(interaction) is False
    assert len(sent) == 1
    sent[0]["view"].stop()

    # Audit Log ghi `TOS_CONSENT_REVOKED`
    logs = await container.audit_service.list_logs(limit=20)
    assert any(entry.get("action") == "TOS_CONSENT_REVOKED" for entry in logs)


@pytest.mark.asyncio
async def test_discord_id_blacklist_blocks_bot_and_me_with_reason_and_forbids_admin_ban(fake_db, fake_gemini, fake_bot):
    """Blacklist chặn ưu tiên cao nhất trên Bot (kèm lý do & rate-limit 60s), chặn `/api/me` + `/me` HTTP 403 và cấm chặn Admin."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-blacklist-32chars-long",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)
    await container.blacklist_repo.attach_db(fake_db)
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")

    admin_uid = "111111111111111111"
    banned_uid = "222222222222222222"
    await container.permission_service.upsert_permission(
        admin_uid, label="Owner", is_admin=True, can_dashboard=True, can_status=True, actor="system"
    )
    await container.permission_service.upsert_permission(
        banned_uid, label="OperatorUser", is_admin=False, can_dashboard=True, can_status=True, actor="system"
    )

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        # 1) Cấm thêm Admin vào blacklist -> HTTP 400
        forbid_resp = client.post(
            "/api/permissions/blacklist",
            json={"discord_user_id": admin_uid, "reason": "Thử cấm Admin"},
        )
        assert forbid_resp.status_code == 400
        assert "Admin" in forbid_resp.json()["detail"]

        # 2) Thêm user thường vào blacklist
        ban_resp = client.post(
            "/api/permissions/blacklist",
            json={"discord_user_id": banned_uid, "reason": "Spam lệnh /ask liên tục"},
        )
        assert ban_resp.status_code == 200
        assert container.blacklist_repo.get_reason_fast(banned_uid) == "Spam lệnh /ask liên tục"

        # 3) Danh sách đen trả về đúng bản ghi và múi giờ IP
        list_resp = client.get(
            "/api/permissions/blacklist",
            headers={"CF-Timezone": "Asia/Ho_Chi_Minh"},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["discord_user_id"] == banned_uid
        assert items[0]["reason"] == "Spam lệnh /ask liên tục"

        # 4) Bot chặn ngay interaction + message kèm lý do chính xác (kể cả khi DND đang tắt!)
        interaction, sent = _make_fake_interaction(user_id=int(banned_uid), command_name="ask")
        assert await container.maintenance_gate.check_interaction(interaction) is False
        assert len(sent) == 1
        assert sent[0]["ephemeral"] is True
        assert "🚫 **Tài khoản của bạn đã bị hạn chế sử dụng KiwiKiwi Bot.**" in sent[0]["content"]
        assert "**Lý do:** Spam lệnh /ask liên tục" in sent[0]["content"]

        # Gửi spam thêm 3 lần trong 60s -> vẫn bị chặn nhưng Audit Log chỉ ghi 1 lần (rate limit 60s)
        for _ in range(3):
            msg, replied = _make_fake_message(user_id=int(banned_uid))
            assert await container.maintenance_gate.check_message(msg) is False
            assert "**Lý do:** Spam lệnh /ask liên tục" in replied[0]["content"]

        logs = await container.audit_service.list_logs(limit=30)
        block_logs = [e for e in logs if e.get("action") == "BLACKLIST_REQUEST_BLOCKED"]
        assert len(block_logs) == 1
        assert any(e.get("action") == "USER_BLACKLISTED" for e in logs)

        # 5) Người dùng Discord bị blacklist gọi `/api/me` và `/me` -> HTTP 403 kèm lý do
        banned_cookie = container.auth_deps.signer.encode({
            "auth_type": "discord",
            "discord_id": banned_uid,
            "username": "banned_op",
            "display_name": "Banned Operator",
            "role": "operator",
            "is_admin": False,
        })
        from app.features.auth.dependencies import SESSION_COOKIE_NAME

        with TestClient(web_app) as banned_client:
            banned_client.cookies.set(SESSION_COOKIE_NAME, banned_cookie)
            me_api = banned_client.get("/api/me")
            assert me_api.status_code == 403
            assert "Spam lệnh /ask liên tục" in me_api.json()["detail"]

            me_page = banned_client.get("/me", follow_redirects=False)
            assert me_page.status_code == 403
            assert "Spam lệnh /ask liên tục" in me_page.text

        # 6) Gỡ cấm -> RAM cache xóa ngay và ghi Audit Log `USER_UNBLACKLISTED`
        unban_resp = client.delete(f"/api/permissions/blacklist/{banned_uid}")
        assert unban_resp.status_code == 200
        assert container.blacklist_repo.get_reason_fast(banned_uid) is None

        interaction_after, _ = _make_fake_interaction(user_id=int(banned_uid), command_name="ask")
        assert await container.maintenance_gate.check_interaction(interaction_after) is True
        logs_after = await container.audit_service.list_logs(limit=30)
        assert any(e.get("action") == "USER_UNBLACKLISTED" for e in logs_after)


@pytest.mark.asyncio
async def test_public_legal_author_attribution_discord_and_local_admin_across_all_5_routes(fake_db, fake_gemini, fake_bot):
    """Kiểm tra hiển thị `Author: [Avatar] {Tên} Lúc {HH:mm DD/MM/YYYY} (based on your IP)` cho cả Discord OAuth2 Admin và Admin mật khẩu trên cả 5 tuyến pháp lý."""
    from app.features.auth.dependencies import SESSION_COOKIE_NAME

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-author-attribution-32chars",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)
    await container.legal_doc_repo.attach_db(fake_db)
    await container.auth_service.create_user("tranzduynghia", "AdminPass123!", role="admin")

    discord_admin_id = "1240503090185109536"
    discord_avatar_url = f"https://cdn.discordapp.com/avatars/{discord_admin_id}/a_demoavatar123.png?size=64"
    await container.permission_service.upsert_permission(
        discord_admin_id, label="DiscordAdmin", is_admin=True, can_dashboard=True, can_status=True, actor="system"
    )

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    all_routes = ("toslinks", "legal", "terms", "policies", "guidelines")

    with TestClient(web_app) as client:
        # 1) Trước khi Admin sửa: hiển thị fallback "Hệ thống KiwiKiwi" và avatar mặc định (không có <img> Discord)
        for slug in all_routes:
            res = client.get(f"/{slug}", headers={"CF-Timezone": "Asia/Ho_Chi_Minh"})
            assert res.status_code == 200
            assert "Author:" in res.text
            assert "Hệ thống KiwiKiwi" in res.text
            assert "js-author-avatar" not in res.text

        # 2) Admin đăng nhập bằng mật khẩu cục bộ lưu /terms và /policies -> tên tài khoản Admin + avatar SVG mặc định (avatar_url=None)
        assert client.post("/auth/login", json={"username": "tranzduynghia", "password": "AdminPass123!"}).status_code == 200
        for slug in ("terms", "policies"):
            save_res = client.put(
                "/api/legal",
                json={
                    "doc_key": slug,
                    "title": f"Tài liệu {slug} cập nhật",
                    "subtitle": "Phiên bản chuẩn",
                    "body": f"<h2>Mục 1</h2><p>Nội dung cập nhật bởi tài khoản cục bộ cho {slug}.</p>",
                },
            )
            assert save_res.status_code == 200
            page_res = client.get(f"/{slug}", headers={"CF-Timezone": "Asia/Ho_Chi_Minh"})
            assert page_res.status_code == 200
            assert "tranzduynghia" in page_res.text
            assert "(based on your IP)" in page_res.text
            assert "data-utc-ts=" in page_res.text
            assert "js-author-avatar" not in page_res.text

        # 3) Admin đăng nhập qua Discord OAuth2 lưu /toslinks, /legal và /guidelines -> tên Discord + <img src="https://cdn.discordapp.com/avatars/...">
        discord_cookie = container.auth_deps.signer.encode({
            "auth_type": "discord",
            "discord_id": discord_admin_id,
            "username": "nghia_discord",
            "display_name": "Nghĩa Admin",
            "avatar_url": discord_avatar_url,
            "role": "admin",
            "is_admin": True,
        })
        with TestClient(web_app) as discord_client:
            discord_client.cookies.set(SESSION_COOKIE_NAME, discord_cookie)
            for slug in ("toslinks", "legal", "guidelines"):
                save_res = discord_client.put(
                    "/api/legal",
                    json={
                        "doc_key": slug,
                        "title": f"Tài liệu {slug} Discord",
                        "subtitle": "Cập nhật qua OAuth2",
                        "body": f"<h2>Mục 1</h2><p>Nội dung cập nhật bởi Discord OAuth2 Admin cho {slug}.</p>",
                    },
                )
                assert save_res.status_code == 200
                page_res = discord_client.get(f"/{slug}", headers={"CF-Timezone": "America/Los_Angeles"})
                assert page_res.status_code == 200
                assert "Nghĩa Admin" in page_res.text
                assert discord_avatar_url in page_res.text
                assert "(based on your IP)" in page_res.text
                assert "data-utc-ts=" in page_res.text

