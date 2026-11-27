"""Kiểm thử toàn diện cho Cổng bảo trì chế độ Không làm phiền (DND Maintenance Mode Gate)."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from fastapi.testclient import TestClient

from app.bot.client import AskBot, DndCommandTree
from app.bot.maintenance_gate import DND_BLOCK_NOTICE, MaintenanceGate
from app.web.app import create_dashboard_app


class FakeInteractionResponse:
    """Mô phỏng `interaction.response` của discord.py để kiểm tra phản hồi `ephemeral=True`."""

    def __init__(self) -> None:
        self._done = False
        self.messages: list[tuple[str, bool]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self._done = True
        self.messages.append((content, ephemeral))


class FakeFollowup:
    """Mô phỏng `interaction.followup` khi phản hồi ban đầu đã kết thúc."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))


def _make_interaction(
    *,
    user_id: int,
    display_name: str = "Normal User",
    command_name: str | None = "ask",
    interaction_type: discord.InteractionType = discord.InteractionType.application_command,
    custom_id: str | None = None,
) -> Any:
    user = SimpleNamespace(id=user_id, name=display_name, display_name=display_name)
    cmd = SimpleNamespace(name=command_name, qualified_name=command_name) if command_name else None
    data: dict[str, Any] = {}
    if command_name:
        data["name"] = command_name
    if custom_id:
        data["custom_id"] = custom_id
    return SimpleNamespace(
        user=user,
        command=cmd,
        data=data,
        type=interaction_type,
        channel_id=987654321,
        response=FakeInteractionResponse(),
        followup=FakeFollowup(),
    )


def _make_message(
    *,
    author_id: int,
    display_name: str = "Normal User",
    guild: Any = None,
    mentions: list[Any] | None = None,
    reply_author_id: int | None = None,
    channel_id: int = 123456,
) -> Any:
    replies: list[dict[str, Any]] = []

    async def _reply(content: str, **kwargs: Any) -> None:
        replies.append({"content": content, **kwargs})

    ref = None
    if reply_author_id is not None:
        ref = SimpleNamespace(resolved=SimpleNamespace(author=SimpleNamespace(id=reply_author_id)))

    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, name=display_name, display_name=display_name, bot=False),
        guild=guild,
        mentions=mentions or [],
        reference=ref,
        channel=SimpleNamespace(id=channel_id),
        reply=_reply,
        replies=replies,
    )


async def _attach_db_and_admin(container, fake_db):
    """Gắn FakeDatabase (tự động tạo tài khoản admin đầu tiên từ INITIAL_ADMIN_*)."""
    await container.sync_coordinator.attach_db(fake_db)
    return container


def _login_admin(client: TestClient) -> None:
    """Đăng nhập tài khoản admin mặc định lấy cookie phiên Dashboard."""
    resp = client.post("/auth/login", json={"username": "admin", "password": "AdminPassword123!"}, follow_redirects=False)
    assert resp.status_code in (200, 302)


@pytest.mark.asyncio
async def test_dashboard_presence_dnd_sync_audit_and_banner(
    monkeypatch,
    caplog,
    app_container,
    fake_db,
    fake_bot,
):
    """Đổi trạng thái online -> dnd -> online qua Dashboard cập nhật tức thì Presence, cờ bảo trì, Audit Log và banner HTML."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "AdminPassword123!")
    await _attach_db_and_admin(app_container, fake_db)

    app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    client = TestClient(app)
    _login_admin(client)

    caplog.set_level(logging.INFO, logger="gemini-bot.maintenance")

    # 1) Bật chế độ Không làm phiền (DND) qua API Dashboard
    res_dnd = client.put(
        "/api/config/presence",
        json={
            "status": "dnd",
            "custom_status": "Đang bảo trì hệ thống",
            "activity_type": "none",
            "activity_text": "",
            "auto_rotate": False,
        },
    )
    assert res_dnd.status_code == 200
    body_dnd = res_dnd.json()
    assert body_dnd["ok"] is True
    assert body_dnd["applied_live"] is True
    assert body_dnd["bot_status"] == "dnd"
    assert body_dnd["maintenance_mode"] is True

    # Cờ RAM và Discord Presence cập nhật tức thì
    assert app_container.maintenance_gate.is_dnd is True
    assert app_container.maintenance_gate.bot_status == "dnd"
    assert fake_bot.presence_updates[-1][0] is discord.Status.dnd

    # Log ứng dụng [BOT_STATUS_CHANGE] và Audit Log MongoDB đầy đủ trường
    assert any("[BOT_STATUS_CHANGE]" in rec.message and "to_status=dnd" in rec.message for rec in caplog.records)
    status_audits = [d for d in fake_db["audit_logs"].docs if d.get("action") == "BOT_STATUS_CHANGE"]
    assert len(status_audits) == 1
    assert status_audits[0]["from_status"] == "online"
    assert status_audits[0]["to_status"] == "dnd"
    assert status_audits[0]["source"] == "Dashboard"
    assert status_audits[0]["actor"]
    assert status_audits[0]["timestamp"]

    # /api/status và SSR /dashboard hiển thị banner & badge DND (và không rò comment HTML)
    status_json = client.get("/api/status").json()
    assert status_json["bot"]["presence_status"] == "dnd"
    assert status_json["bot"]["maintenance_mode"] is True

    html_dnd = client.get("/dashboard").text
    assert "Chế độ bảo trì DND đang bật" in html_dnd
    assert 'id="dnd-maintenance-banner"' in html_dnd
    assert 'id="hdr-dnd-badge"' in html_dnd
    assert "<!--" not in html_dnd

    # 2) Tắt chế độ DND về online
    res_online = client.put(
        "/api/config/presence",
        json={
            "status": "online",
            "custom_status": "Sẵn sàng phục vụ",
            "activity_type": "listening",
            "activity_text": "/ask",
            "auto_rotate": False,
        },
    )
    assert res_online.status_code == 200
    assert res_online.json()["bot_status"] == "online"
    assert res_online.json()["maintenance_mode"] is False
    assert app_container.maintenance_gate.is_dnd is False
    assert fake_bot.presence_updates[-1][0] is discord.Status.online

    status_audits = [d for d in fake_db["audit_logs"].docs if d.get("action") == "BOT_STATUS_CHANGE"]
    assert len(status_audits) == 2
    assert status_audits[1]["from_status"] == "dnd"
    assert status_audits[1]["to_status"] == "online"


@pytest.mark.asyncio
async def test_dnd_command_tree_blocks_non_admin_and_throttles_db_audit(
    caplog,
    app_container,
    fake_db,
):
    """DndCommandTree chặn non-admin trước khi chạy handler/Gemini, gửi ephemeral và giới hạn 1 DB log/user mỗi 60s."""
    await app_container.sync_coordinator.attach_db(fake_db)
    clock = [1000.0]
    gate = MaintenanceGate(
        config_repo=app_container.config_repo,
        audit_service=app_container.audit_service,
        permission_service=app_container.permission_service,
        admin_discord_id="1240503090185109536",
        block_log_throttle_seconds=60.0,
        monotonic_fn=lambda: clock[0],
    )
    gate.sync_from_config({"status": "dnd"})
    assert gate.is_dnd is True

    discord_client = discord.Client(intents=discord.Intents.default())
    tree = DndCommandTree(discord_client, maintenance_gate=gate)
    caplog.set_level(logging.INFO, logger="gemini-bot.maintenance")

    # 5 yêu cầu liên tiếp từ cùng 1 user thường trong vòng 10 giây
    for _ in range(5):
        interaction = _make_interaction(user_id=555111, display_name="Regular Member", command_name="ask")
        allowed = await tree.interaction_check(interaction)
        assert allowed is False
        assert interaction.response.messages == [(DND_BLOCK_NOTICE, True)]

    # 5 log Python [DND_BLOCK], nhưng CHỈ ĐÚNG 1 bản ghi MongoDB DND_REQUEST_BLOCKED trong cửa sổ 60s
    block_py_logs = [rec for rec in caplog.records if "[DND_BLOCK]" in rec.message and "user_id=555111" in rec.message]
    assert len(block_py_logs) == 5

    block_db_logs = [d for d in fake_db["audit_logs"].docs if d.get("action") == "DND_REQUEST_BLOCKED"]
    assert len(block_db_logs) == 1
    assert block_db_logs[0]["user_id"] == "555111"
    assert block_db_logs[0]["command_or_channel"] == "/ask"

    # Sau 61 giây -> cho phép ghi thêm bản ghi MongoDB thứ 2
    clock[0] += 61.0
    interaction_after = _make_interaction(user_id=555111, display_name="Regular Member", command_name="ask")
    assert await tree.interaction_check(interaction_after) is False
    block_db_logs = [d for d in fake_db["audit_logs"].docs if d.get("action") == "DND_REQUEST_BLOCKED"]
    assert len(block_db_logs) == 2

    # Khi dọn cache stale throttles sau thêm 65 giây -> xóa sạch key cũ tiết kiệm RAM
    clock[0] += 65.0
    purged = gate.purge_stale_throttles()
    assert purged == 1


@pytest.mark.asyncio
async def test_dnd_admin_bypass_slash_component_and_message(
    caplog,
    app_container,
    fake_db,
):
    """Admin khớp ADMIN_DISCORD_ID vượt qua khóa DND ở slash command, component và on_message, đồng thời ghi [DND_ADMIN_BYPASS]."""
    await app_container.sync_coordinator.attach_db(fake_db)
    gate = MaintenanceGate(
        config_repo=app_container.config_repo,
        audit_service=app_container.audit_service,
        permission_service=app_container.permission_service,
        admin_discord_id="1240503090185109536",
    )
    gate.sync_from_config({"status": "dnd"})
    discord_client = discord.Client(intents=discord.Intents.default())
    tree = DndCommandTree(discord_client, maintenance_gate=gate)

    caplog.set_level(logging.INFO, logger="gemini-bot.maintenance")

    # 1) Slash command /ask
    admin_slash = _make_interaction(user_id=1240503090185109536, display_name="Owner Admin", command_name="ask")
    assert await tree.interaction_check(admin_slash) is True
    assert admin_slash.response.messages == []

    # 2) Component / Button interaction
    admin_btn = _make_interaction(
        user_id=1240503090185109536,
        display_name="Owner Admin",
        command_name=None,
        interaction_type=discord.InteractionType.component,
        custom_id="session_continue_btn",
    )
    assert await gate.check_interaction(admin_btn) is True
    assert admin_btn.response.messages == []

    # 3) Active message / DM
    admin_msg = _make_message(author_id=1240503090185109536, display_name="Owner Admin", guild=None)
    assert await gate.check_message(admin_msg) is True
    assert admin_msg.replies == []

    bypass_logs = [rec for rec in caplog.records if "[DND_ADMIN_BYPASS]" in rec.message]
    assert len(bypass_logs) == 3
    assert gate.status_snapshot()["admin_bypass_count"] == 3


@pytest.mark.asyncio
async def test_dnd_on_interaction_and_on_message_guards(
    app_container,
    fake_db,
):
    """AskBot.on_interaction và AskBot.on_message chặn tương tác nút bấm, DM, nhắc tên và trả lời bot của user thường khi bật DND."""
    await app_container.sync_coordinator.attach_db(fake_db)
    app_container.maintenance_gate.sync_from_config({"status": "dnd"})

    bot_stub = SimpleNamespace(
        container=app_container,
        user=SimpleNamespace(id=999999999999999999),
    )

    # Component interaction của user thường -> bị chặn kèm thông báo ephemeral
    comp_inter = _make_interaction(
        user_id=777123,
        command_name=None,
        interaction_type=discord.InteractionType.component,
        custom_id="ask_retry_btn",
    )
    await AskBot.on_interaction(bot_stub, comp_inter)
    assert comp_inter.response.messages == [(DND_BLOCK_NOTICE, True)]

    # Tin nhắn thường trong server (không nhắc bot, không reply bot) -> KHÔNG gửi thông báo làm phiền
    guild_obj = SimpleNamespace(id=42)
    unrelated_msg = _make_message(author_id=777123, guild=guild_obj, mentions=[])
    await AskBot.on_message(bot_stub, unrelated_msg)
    assert unrelated_msg.replies == []

    # Nhắc tên bot trong server -> bị chặn với DND_BLOCK_NOTICE
    mention_msg = _make_message(
        author_id=777123,
        guild=guild_obj,
        mentions=[SimpleNamespace(id=999999999999999999)],
    )
    await AskBot.on_message(bot_stub, mention_msg)
    assert len(mention_msg.replies) == 1
    assert mention_msg.replies[0]["content"] == DND_BLOCK_NOTICE

    # Trả lời tin nhắn bot trong server -> bị chặn
    reply_msg = _make_message(
        author_id=777124,
        guild=guild_obj,
        reply_author_id=999999999999999999,
    )
    await AskBot.on_message(bot_stub, reply_msg)
    assert len(reply_msg.replies) == 1
    assert reply_msg.replies[0]["content"] == DND_BLOCK_NOTICE

    # Nhắn DM trực tiếp -> bị chặn
    dm_msg = _make_message(author_id=777125, guild=None)
    await AskBot.on_message(bot_stub, dm_msg)
    assert len(dm_msg.replies) == 1
    assert dm_msg.replies[0]["content"] == DND_BLOCK_NOTICE


@pytest.mark.asyncio
async def test_atlas_external_sync_dnd_updates_gate_and_audits(
    app_container,
    fake_db,
):
    """Khi trạng thái `presence.status` bị chỉnh trực tiếp thành `dnd` trên MongoDB Atlas, Watcher tự đồng bộ cờ bảo trì và ghi Audit Log."""
    await app_container.sync_coordinator.attach_db(fake_db)
    # Chuẩn bị bản ghi bot_config trên Atlas giả
    cfg_doc = dict(app_container.config_repo.current_config)
    cfg_doc["_id"] = "global"
    cfg_doc["presence"] = {
        **cfg_doc.get("presence", {}),
        "status": "dnd",
    }
    await fake_db["bot_config"].replace_one({"_id": "global"}, cfg_doc, upsert=True)

    result = await app_container.atlas_config_watcher.poll_once()
    assert result["changed"] is True
    assert app_container.maintenance_gate.is_dnd is True
    assert app_container.maintenance_gate.bot_status == "dnd"

    status_audits = [d for d in fake_db["audit_logs"].docs if d.get("action") == "BOT_STATUS_CHANGE"]
    assert len(status_audits) == 1
    assert status_audits[0]["from_status"] == "online"
    assert status_audits[0]["to_status"] == "dnd"
    assert "Atlas" in str(status_audits[0].get("source") or "")
