"""Kiểm thử hồi quy `/ask`: khóa DND cho phiên mới/phiên tiếp diễn, Hot-Reload và lưới an toàn sau defer()."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from app.bot.client import DndCommandTree
from app.bot.maintenance_gate import DND_BLOCK_NOTICE, MaintenanceGate
from app.features.chat import commands as chat_commands


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.messages: list[tuple[str, bool]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, ephemeral: bool = False, **_: Any) -> None:
        self._done = True
        self.messages.append((content, ephemeral))

    async def defer(self, **_: Any) -> None:
        self._done = True


class _Followup:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.fail = fail

    async def send(self, content: str | None = None, *, ephemeral: bool = False, **_: Any) -> None:
        if self.fail:
            raise RuntimeError("followup hỏng")
        self.messages.append((content or "", ephemeral))


def _interaction(uid: int, *, followup_fail: bool = False) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=uid, name="u", display_name="u", roles=[], display_avatar=SimpleNamespace(url="")),
        guild=None,
        command=SimpleNamespace(name="ask", qualified_name="ask"),
        data={"name": "ask"},
        type=discord.InteractionType.application_command,
        channel_id=1,
        extras={},
        response=_Response(),
        followup=_Followup(fail=followup_fail),
    )


class _Cooldowns:
    def remaining_seconds(self, *_a: Any) -> float:
        return 0.0

    def mark_used(self, *_a: Any) -> None:
        pass


class _Lock:
    async def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


class _Locks:
    def get(self, _uid: int) -> _Lock:
        return _Lock()


class _Semaphore:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _Stats:
    def __init__(self) -> None:
        self.records: list[Any] = []

    def record(self, *args: Any) -> None:
        self.records.append(args)


class _Registry:
    def auto_chain(self) -> list[str]:
        return ["m1"]

    def display_name_for(self, mid: str) -> str:
        return mid


class _Gemini:
    def __init__(self) -> None:
        self.calls = 0

    async def call_with_fallback(self, *_a: Any, **_k: Any):
        self.calls += 1
        raise RuntimeError("Gemini nổ bất ngờ")


class _Sessions:
    enabled = False
    ok = False


def _build(config_repo, gate, gemini):
    discord_client = discord.Client(intents=discord.Intents.default())
    tree = DndCommandTree(discord_client, maintenance_gate=gate)
    module = importlib.reload(chat_commands)  # mô phỏng Hot-Reload
    module.register_chat_commands(
        tree,
        config_repo=config_repo,
        model_registry=_Registry(),
        model_cooldowns=SimpleNamespace(clear_model=lambda *_: None, mark_failure=lambda *_: None),
        gemini_service=gemini,
        session_repo=_Sessions(),
        usage_stats=_Stats(),
        user_cooldowns=_Cooldowns(),
        user_locks=_Locks(),
        gemini_semaphore=_Semaphore(),
        maintenance_gate=gate,
    )
    return tree, tree.get_command("ask")


@pytest.mark.asyncio
async def test_dnd_blocks_non_admin_before_defer_and_gemini_but_admin_bypasses(app_container):
    """DND chặn user thường (phiên mới lẫn tiếp diễn) trước defer/Gemini; Admin vẫn đi qua."""
    gate = MaintenanceGate(
        config_repo=app_container.config_repo,
        audit_service=app_container.audit_service,
        permission_service=app_container.permission_service,
        admin_discord_id="999",
    )
    gate.sync_from_config({"status": "dnd"})
    gemini = _Gemini()
    _tree, ask = _build(app_container.config_repo, gate, gemini)

    # Gọi trực tiếp callback (bỏ qua interaction_check) — lớp phòng thủ trong /ask phải tự chặn
    for _turn in range(3):
        inter = _interaction(555)
        await ask.callback(inter, prompt="xin chào")
        assert inter.response.messages == [(DND_BLOCK_NOTICE, True)]
        assert inter.followup.messages == []
    assert gemini.calls == 0

    admin = _interaction(999)
    await ask.callback(admin, prompt="test")
    assert gemini.calls == 1  # Admin vượt DND, lỗi Gemini được báo ephemeral
    assert admin.followup.messages and all(eph for _m, eph in admin.followup.messages)


@pytest.mark.asyncio
async def test_external_presence_change_is_picked_up_live(app_container):
    """Đổi presence.status trong cấu hình (Dashboard/atlas) có hiệu lực ngay dù không gọi sync_from_config."""
    gate = MaintenanceGate(
        config_repo=app_container.config_repo,
        audit_service=app_container.audit_service,
        permission_service=app_container.permission_service,
    )
    assert gate.is_dnd is False
    app_container.config_repo.current_config.setdefault("presence", {})["status"] = "dnd"
    assert gate.is_dnd is True
    app_container.config_repo.current_config["presence"]["status"] = "online"
    assert gate.is_dnd is False


@pytest.mark.asyncio
async def test_hot_reload_then_ask_error_is_ephemeral_and_never_hangs(app_container):
    """Sau Hot-Reload, lỗi bất ngờ sau defer() luôn gửi ephemeral; ngay cả khi followup hỏng cũng không ném lỗi."""
    gate = MaintenanceGate(
        config_repo=app_container.config_repo,
        audit_service=app_container.audit_service,
        permission_service=app_container.permission_service,
    )
    gemini = _Gemini()
    _tree, ask = _build(app_container.config_repo, gate, gemini)

    inter = _interaction(321)
    await ask.callback(inter, prompt="hỏi gì đó")
    assert gemini.calls == 1
    assert inter.followup.messages and inter.followup.messages[-1][1] is True

    broken = _interaction(322, followup_fail=True)
    await ask.callback(broken, prompt="hỏi gì đó")  # không được ném ngoại lệ
