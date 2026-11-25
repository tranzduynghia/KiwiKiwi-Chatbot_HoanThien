"""Kiểm thử cơ chế Write-Behind RAM Sync Buffer: đệm khi offline -> đồng bộ & giải phóng RAM khi kết nối Atlas."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_write_behind_buffer_flushes_and_releases_ram(app_container, fake_db):
    """Mọi thay đổi khi chưa gắn DB phải được áp dụng ngay trên RAM, giữ trong hàng đợi và xả sạch khi gắn DB."""
    # 1) Thực hiện thay đổi khi chưa kết nối MongoDB Atlas (chế độ đệm RAM)
    await app_container.config_service.update_config(
        {"ask": {"user_cooldown": 3.5}},
        updated_by="admin",
        section="ask",
        summary="Chỉnh cooldown offline",
    )
    await app_container.permission_service.upsert_permission(
        "987654321012345678",
        label="Mod offline",
        can_status=True,
        actor="admin",
    )
    await app_container.auth_service.create_user(
        "offline_admin",
        "MatKhau123!",
        role="admin",
        actor="admin",
    )

    status_before = app_container.sync_coordinator.sync_status()
    assert status_before["pending_config"] is True
    assert status_before["pending_perms"] == 1
    assert status_before["pending_users"] == 1
    assert status_before["pending_audits"] == 3
    assert status_before["pending_count"] == 6

    # Dữ liệu phải có hiệu lực ngay trên RAM trước cả khi gắn DB
    assert app_container.config_repo.ask_cfg()["user_cooldown"] == 3.5
    assert await app_container.permission_service.can_use_status("987654321012345678") is True
    assert await app_container.auth_service.authenticate_password("offline_admin", "MatKhau123!") is not None

    # 2) Gắn DB giả -> tự động đẩy toàn bộ bộ đệm RAM lên DB và giải phóng RAM
    await app_container.sync_coordinator.attach_db(fake_db)

    status_after = app_container.sync_coordinator.sync_status()
    assert status_after["pending_count"] == 0
    assert status_after["pending_config"] is False
    assert status_after["pending_perms"] == 0
    assert status_after["pending_users"] == 0
    assert status_after["pending_audits"] == 0
    assert status_after["last_flush_at"] is not None
    assert status_after["last_flush_error"] is None

    # Xác minh dữ liệu đã thực sự ghi vào các collection MongoDB tương ứng
    stored_cfg = await fake_db["bot_config"].find_one({"_id": "global"})
    assert stored_cfg["ask"]["user_cooldown"] == 3.5
    stored_perm = await fake_db["discord_permissions"].find_one({"_id": "987654321012345678"})
    assert stored_perm["can_status"] is True
    stored_user = await fake_db["dashboard_users"].find_one({"username": "offline_admin"})
    assert stored_user is not None and stored_user["password_hash"].startswith("scrypt$")
    assert await fake_db["audit_logs"].count_documents({}) == 3
