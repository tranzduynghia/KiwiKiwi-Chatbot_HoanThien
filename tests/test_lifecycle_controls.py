"""Kiểm thử toàn diện cho Hot-Reload Module Lệnh (kèm dọn RAM), Soft-Restart Container (HTTP 202), RBAC Admin và lệnh `/config`."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from fastapi.testclient import TestClient

from app.bot.client import AskBot
from app.bot.lifecycle import (
    COMMAND_MODULE_NAMES,
    LifecycleController,
    LifecycleCooldownError,
)
from app.features.config.commands import AdminConfigConsoleView, LifecycleConfirmView
from app.web.app import create_dashboard_app


class FakeDiscordResponse:
    """Mô phỏng `interaction.response` cho kiểm thử lệnh `/config` và View xác nhận 2 bước."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []

    def is_done(self) -> bool:
        return bool(self.sent or self.edited)

    async def send_message(self, content: str, *, view: Any = None, ephemeral: bool = False) -> None:
        self.sent.append({"content": content, "view": view, "ephemeral": ephemeral})

    async def edit_message(self, *, content: str = "", view: Any = None) -> None:
        self.edited.append({"content": content, "view": view})


def _make_interaction(user_id: int, display_name: str = "Admin User") -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, name=display_name, display_name=display_name, display_avatar=None),
        response=FakeDiscordResponse(),
        followup=SimpleNamespace(send=lambda *a, **kw: None),
    )


@pytest.mark.asyncio
async def test_hot_reload_deduplicates_commands_stops_views_reports_rss_and_enforces_cooldown(
    app_container,
    fake_db,
):
    """Hot-Reload nạp lại đúng 4 module lệnh, không gây trùng lệnh trên CommandTree, dừng View tồn đọng, báo RSS trước/sau và áp dụng cooldown 15s."""
    await app_container.sync_coordinator.attach_db(fake_db)

    rss_samples = iter([185.4, 172.1])
    clock = [100.0]
    lifecycle = LifecycleController(
        audit_service=app_container.audit_service,
        sync_coordinator=app_container.sync_coordinator,
        session_repo=app_container.session_repo,
        reload_cooldown_seconds=15.0,
        monotonic_fn=lambda: clock[0],
        rss_reader=lambda: next(rss_samples, 170.0),
    )
    app_container.lifecycle_controller = lifecycle
    bot = AskBot(app_container)

    # Theo dõi 1 View đang treo để đảm bảo bị dừng trước khi reload
    dangling_view = discord.ui.View(timeout=60)
    lifecycle.track_view(dangling_view)
    assert dangling_view.is_finished() is False

    # Chạy Hot-Reload lần 1
    res = await lifecycle.reload_command_modules(
        bot,
        actor="admin",
        actor_type="password",
        source="web_dashboard",
    )
    assert res["ok"] is True
    assert tuple(res["reloaded_modules"]) == COMMAND_MODULE_NAMES
    assert res["reloaded_count"] == 4
    assert res["stopped_views"] == 1
    assert dangling_view.is_finished() is True
    assert res["rss_before_mb"] == 185.4
    assert res["rss_after_mb"] == 172.1
    assert res["duration_ms"] >= 0.0

    # Kiểm tra CommandTree không hề bị nhân đôi lệnh (ask, refresh, session, status, config)
    cmd_names = [cmd.name for cmd in bot.tree.get_commands()]
    assert len(cmd_names) == len(set(cmd_names))
    for expected in ("ask", "refresh", "session", "status", "config"):
        assert expected in cmd_names

    # Ghi nhận Audit Log COMMAND_MODULES_RELOADED
    reload_audits = [d for d in fake_db["audit_logs"].docs if d.get("action") == "COMMAND_MODULES_RELOADED"]
    assert len(reload_audits) == 1
    assert reload_audits[0]["source"] == "web_dashboard"
    assert reload_audits[0]["rss_before_mb"] == 185.4
    assert reload_audits[0]["rss_after_mb"] == 172.1

    # Gọi lại ngay trong vòng 15 giây -> bị chặn bởi cooldown
    clock[0] += 5.0
    with pytest.raises(LifecycleCooldownError) as exc_info:
        await lifecycle.reload_command_modules(bot, actor="admin", source="web_dashboard")
    assert exc_info.value.status_code == 429

    # Sau 16 giây -> cho phép reload lại và vẫn không trùng lệnh
    clock[0] += 11.0
    res2 = await lifecycle.reload_command_modules(bot, actor="admin", source="web_dashboard")
    assert res2["ok"] is True
    cmd_names_after = [cmd.name for cmd in bot.tree.get_commands()]
    assert len(cmd_names_after) == len(set(cmd_names_after))


@pytest.mark.asyncio
async def test_soft_restart_returns_202_logs_audit_before_exit_and_enforces_rbac(
    monkeypatch,
    app_container,
    fake_db,
    fake_bot,
):
    """API /api/system/reload-commands & /api/system/soft-restart chỉ cho Admin (401/403 với người khác), trả 202 Accepted và đóng bot + exit(0) sau delay."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "AdminPassword123!")
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.auth_service.create_user("operator1", "OperPassword123!", role="operator")

    exit_codes: list[int] = []
    closed_flag = [False]

    async def _fake_close():
        closed_flag[0] = True

    fake_bot.close = _fake_close
    lifecycle = LifecycleController(
        audit_service=app_container.audit_service,
        sync_coordinator=app_container.sync_coordinator,
        session_repo=app_container.session_repo,
        restart_cooldown_seconds=30.0,
        exit_fn=lambda code: exit_codes.append(code),
    )
    app_container.lifecycle_controller = lifecycle

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    client = TestClient(web_app)

    # 1) Chưa đăng nhập -> 401
    assert client.post("/api/system/reload-commands").status_code == 401
    assert client.post("/api/system/soft-restart").status_code == 401

    # 2) Operator (không phải Admin) -> 403
    assert client.post("/auth/login", json={"username": "operator1", "password": "OperPassword123!"}).status_code == 200
    assert client.post("/api/system/reload-commands").status_code == 403
    assert client.post("/api/system/soft-restart").status_code == 403

    # 3) Admin đăng nhập -> /api/system/reload-commands trả 200, /api/system/soft-restart trả 202 Accepted
    assert client.post("/auth/login", json={"username": "admin", "password": "AdminPassword123!"}).status_code == 200
    reload_res = client.post("/api/system/reload-commands")
    assert reload_res.status_code == 200
    assert reload_res.json()["reloaded_count"] == 4

    restart_res = client.post("/api/system/soft-restart")
    assert restart_res.status_code == 202
    assert restart_res.json()["accepted"] is True

    # Lệnh trùng lập tức bị từ chối (single-flight 409)
    dup_res = client.post("/api/system/soft-restart")
    assert dup_res.status_code == 409

    # Audit Log CONTAINER_SOFT_RESTART đã được xả lên MongoDB TRƯỚC khi tiến trình thoát
    restart_audits = [d for d in fake_db["audit_logs"].docs if d.get("action") == "CONTAINER_SOFT_RESTART"]
    assert len(restart_audits) == 1
    assert restart_audits[0]["source"] == "web_dashboard"

    # Kiểm tra /api/health hoạt động cho vòng lặp kết nối lại của Dashboard
    assert client.get("/api/health").status_code == 200

    # Đảm bảo giao diện /dashboard có thẻ Điều khiển Vòng đời Bot và không có comment HTML
    dash_html = client.get("/dashboard").text
    assert 'id="btn-reload-commands"' in dash_html
    assert 'id="btn-soft-restart"' in dash_html
    assert 'id="soft-restart-overlay"' in dash_html
    assert "<!--" not in dash_html

    # Xác minh trình thực thi thoát mượt (flush DB -> bot.close() -> exit_fn(0)) trên vòng lặp asyncio hiện hành
    direct_exit_codes: list[int] = []
    direct_lifecycle = LifecycleController(
        audit_service=app_container.audit_service,
        sync_coordinator=app_container.sync_coordinator,
        session_repo=app_container.session_repo,
        restart_cooldown_seconds=30.0,
        exit_fn=lambda code: direct_exit_codes.append(code),
    )
    await direct_lifecycle.schedule_soft_restart(
        fake_bot,
        actor="admin",
        actor_type="password",
        source="web_dashboard",
        delay_seconds=0.05,
    )
    assert direct_lifecycle._restart_task is not None
    await direct_lifecycle._restart_task
    assert closed_flag[0] is True
    assert direct_exit_codes == [0]


@pytest.mark.asyncio
async def test_discord_config_command_admin_only_and_two_step_confirmation(
    monkeypatch,
    app_container,
    fake_db,
):
    """Lệnh `/config` chặn non-admin và cho phép Admin chạy xác nhận 2 bước ephemeral (timeout=60s, dừng View sạch)."""
    monkeypatch.setenv("ADMIN_DISCORD_ID", "1240503090185109536")
    await app_container.sync_coordinator.attach_db(fake_db)

    exit_codes: list[int] = []
    lifecycle = LifecycleController(
        audit_service=app_container.audit_service,
        sync_coordinator=app_container.sync_coordinator,
        session_repo=app_container.session_repo,
        exit_fn=lambda code: exit_codes.append(code),
    )
    app_container.lifecycle_controller = lifecycle
    bot = AskBot(app_container)
    config_cmd = bot.tree.get_command("config")
    assert config_cmd is not None

    # 1) Non-admin bị từ chối ngay (ephemeral=True)
    non_admin_inter = _make_interaction(user_id=999001, display_name="Regular User")
    await config_cmd.callback(non_admin_inter)
    assert len(non_admin_inter.response.sent) == 1
    assert non_admin_inter.response.sent[0]["ephemeral"] is True
    assert "⛔" in non_admin_inter.response.sent[0]["content"]

    # 2) Admin mở /config -> nhận AdminConfigConsoleView (timeout=60s)
    admin_inter = _make_interaction(user_id=1240503090185109536, display_name="Owner Admin")
    await config_cmd.callback(admin_inter)
    console_view = admin_inter.response.sent[0]["view"]
    assert type(console_view).__name__ == "AdminConfigConsoleView"
    assert console_view.timeout == 60.0

    # Bước 1: Nhấn "Reload Module Lệnh" -> dừng console_view và mở LifecycleConfirmView
    step1_inter = _make_interaction(user_id=1240503090185109536, display_name="Owner Admin")
    await console_view.reload_btn.callback(step1_inter)
    assert console_view.is_finished() is True
    confirm_view = step1_inter.response.edited[0]["view"]
    assert type(confirm_view).__name__ == "LifecycleConfirmView"

    # Bước 2: Nhấn xác nhận Reload -> thực thi Hot-Reload, báo RSS & dừng confirm_view
    step2_inter = _make_interaction(user_id=1240503090185109536, display_name="Owner Admin")
    await confirm_view.confirm_button.callback(step2_inter)
    assert confirm_view.is_finished() is True
    reply_text = step2_inter.response.edited[0]["content"]
    assert "Hot-Reload 4 module lệnh" in reply_text
    assert "RSS" in reply_text

    # Kiểm tra Audit Log ghi nguồn discord_slash
    discord_audits = [
        d for d in fake_db["audit_logs"].docs
        if d.get("action") == "COMMAND_MODULES_RELOADED" and d.get("source") == "discord_slash"
    ]
    assert len(discord_audits) == 1
