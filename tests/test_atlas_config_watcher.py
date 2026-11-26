"""Kiểm thử toàn diện `AtlasConfigWatcher` (poll 5 giây, hot-reload RAM/Tunnel/Middleware & Audit Log nguồn Atlas)."""
from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.cloudflare_tunnel import CloudflareTunnelService
from app.core.settings import AppSettings
from app.features.config.atlas_watcher import (
    ATLAS_EXTERNAL_SYNC_SOURCE,
    DEFAULT_ATLAS_WATCH_INTERVAL_SECONDS,
    compute_config_hash,
)
from app.main import build_container
from app.web.app import create_dashboard_app


class FakeStreamReader:
    """Mô phỏng `asyncio.StreamReader` trả về luồng stderr cho tiến trình con `cloudflared`."""

    def __init__(self, lines: list[bytes]):
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        for line in lines:
            self._queue.put_nowait(line)
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()


class FakeSubprocess:
    """Mô phỏng tiến trình con bất đồng bộ `cloudflared`."""

    def __init__(self, pid: int, stderr_lines: list[bytes]):
        self.pid = pid
        self.returncode: int | None = None
        self.stderr = FakeStreamReader(stderr_lines)
        self.terminated = False
        self._wait_event = asyncio.Event()

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0
        self.stderr.close()
        self._wait_event.set()

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9
        self.stderr.close()
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self.returncode if self.returncode is not None else 0


@pytest.mark.asyncio
async def test_atlas_watcher_defaults_hash_noop_and_pending_guard(fake_db, fake_gemini):
    """Kiểm tra chu kỳ mặc định 5 giây, bỏ qua khi hash không đổi và không ghi đè khi có bản ghi RAM chờ đồng bộ."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-watcher-1",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)

    watcher = container.atlas_config_watcher
    assert watcher.poll_interval_seconds == DEFAULT_ATLAS_WATCH_INTERVAL_SECONDS == 5.0

    # Lần poll khi MongoDB Atlas giống hệt RAM -> không đổi, không ghi Audit Log
    noop = await watcher.poll_once()
    assert noop["changed"] is False
    assert await container.audit_service.list_logs() == []

    # Hash bỏ qua metadata updated_at / updated_by
    cfg_a = copy.deepcopy(container.config_repo.current_config)
    cfg_b = copy.deepcopy(cfg_a)
    cfg_b["updated_at"] = "2099-01-01T00:00:00+00:00"
    cfg_b["updated_by"] = "someone-else"
    assert compute_config_hash(cfg_a) == compute_config_hash(cfg_b)

    # Khi RAM đang có thay đổi nội bộ chờ flush (`pending_count > 0`) -> watcher tự bỏ qua an toàn
    staged = copy.deepcopy(container.config_repo.current_config)
    staged["ask"]["max_concurrent"] = 7
    container.config_repo.stage_config(staged)
    skipped = await watcher.poll_once()
    assert skipped["changed"] is False
    assert skipped["reason"] == "local_pending_write"
    assert container.config_repo.ask_cfg()["max_concurrent"] == 7


@pytest.mark.asyncio
async def test_atlas_external_tunnel_hot_reload_and_zero_cache_middleware(fake_db, fake_gemini, fake_bot):
    """Sửa trực tiếp `bot_config` trên Atlas tự động bật/restart/tắt cloudflared, mở/khóa middleware tức thì và ghi Audit Log che token."""
    spawned_commands: list[tuple[str, ...]] = []
    procs: list[FakeSubprocess] = []

    async def fake_spawn(*cmd, **_kwargs):
        spawned_commands.append(cmd)
        proc = FakeSubprocess(pid=6100 + len(procs), stderr_lines=[b"INF Connected to Cloudflare Edge\n"])
        procs.append(proc)
        return proc

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-watcher-2",
        port=10000,
    )
    tunnel_service = CloudflareTunnelService(
        local_port=10000,
        spawn_subprocess=fake_spawn,
        quick_url_wait_seconds=0.5,
    )
    container = build_container(
        settings,
        gemini_client=fake_gemini,
        reserve_buffer_bytes=16 * 1024,
        cloudflare_tunnel=tunnel_service,
    )
    await container.sync_coordinator.attach_db(fake_db)
    await container.session_repo.attach_db(fake_db)
    await container.model_registry.refresh()
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")

    callback_events: list[int] = []
    container.config_service.on_update(lambda cfg: callback_events.append(int(cfg["ask"]["max_concurrent"])))

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    watcher = container.atlas_config_watcher

    with TestClient(web_app) as client:
        client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        assert client.get("/api/status").status_code == 200

        # 1) Sửa trực tiếp trong MongoDB Atlas: bật Named Tunnel + bật khóa tunnel_only_inbound + đổi ask.max_concurrent
        token_v1 = "eyJ-atlas-manual-token-v1-secret"
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        assert remote_doc is not None
        remote_doc["tunnel"]["cf_tunnel_enabled"] = True
        remote_doc["tunnel"]["cf_tunnel_mode"] = "named"
        remote_doc["tunnel"]["cf_tunnel_token"] = token_v1
        remote_doc["tunnel"]["tunnel_only_inbound"] = True
        remote_doc["ask"]["max_concurrent"] = 5
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        sync1 = await watcher.poll_once()
        assert sync1["changed"] is True
        assert sync1["tunnel_reloaded"] is True
        assert len(spawned_commands) == 1
        assert token_v1 in spawned_commands[0]
        assert tunnel_service.is_running is True
        assert callback_events[-1] == 5

        # Middleware đọc động `current_config` -> request kế tiếp không có header CF lập tức bị chặn 403
        assert client.get("/api/status").status_code == 403
        assert client.get("/health").status_code == 200
        assert client.get("/api/status", headers={"Cf-Ray": "atlas-sync-ray-1"}).status_code == 200

        # 2) Chỉ tắt `tunnel_only_inbound` trực tiếp trên Atlas (tình huống cứu hộ .code.run) -> mở khóa tức thì KHÔNG restart cloudflared
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        remote_doc["tunnel"]["tunnel_only_inbound"] = False
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        sync2 = await watcher.poll_once()
        assert sync2["changed"] is True
        assert sync2["tunnel_reloaded"] is False
        assert len(spawned_commands) == 1  # Không khởi động lại cloudflared khi chỉ đổi khóa inbound
        assert tunnel_service.status_snapshot()["tunnel_only_inbound"] is False
        # Request trực tiếp (không có header CF) mở lại 200 ngay lập tức trên cùng instance FastAPI
        assert client.get("/api/status").status_code == 200

        # 3) Đổi CHỈ `cf_tunnel_token` trực tiếp trên Atlas -> tự động hot-restart cloudflared với token mới & che token trong Audit Log
        token_v2 = "eyJ-atlas-manual-token-v2-rotated"
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        remote_doc["tunnel"]["cf_tunnel_token"] = token_v2
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        sync3 = await watcher.poll_once()
        assert sync3["changed"] is True
        assert sync3["tunnel_reloaded"] is True
        assert procs[0].terminated is True
        assert len(spawned_commands) == 2
        assert token_v2 in spawned_commands[1]
        assert tunnel_service.is_running is True

        # 4) Tắt `cf_tunnel_enabled` trực tiếp trên Atlas -> tự động dừng tiến trình cloudflared
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        remote_doc["tunnel"]["cf_tunnel_enabled"] = False
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        sync4 = await watcher.poll_once()
        assert sync4["changed"] is True
        assert sync4["tunnel_reloaded"] is True
        assert procs[1].terminated is True
        assert tunnel_service.is_running is False

        # 5) Hỗ trợ cả trường hợp người dùng đặt `tunnel_only_inbound` phẳng ở gốc tài liệu `bot_config`
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        remote_doc["tunnel_only_inbound"] = True
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        sync5 = await watcher.poll_once()
        assert sync5["changed"] is True
        assert client.get("/api/status").status_code == 403
        assert client.get("/api/status", headers={"CF-Connecting-IP": "198.51.100.2"}).status_code == 200

        # Kiểm tra Audit Log đã ghi lại các bản ghi có `source == "MongoDB Atlas External Sync"` và KHÔNG lộ token gốc
        audit_logs = await container.audit_service.list_logs(limit=20)
        atlas_logs = [entry for entry in audit_logs if entry.get("source") == ATLAS_EXTERNAL_SYNC_SOURCE]
        assert len(atlas_logs) == 5
        serialized_logs = str(atlas_logs)
        assert token_v1 not in serialized_logs
        assert token_v2 not in serialized_logs
        assert any(
            change.get("field") == "tunnel.cf_tunnel_token" and "•••••" in str(change.get("after", ""))
            for entry in atlas_logs
            for change in (entry.get("changes") or [])
        )


@pytest.mark.asyncio
async def test_atlas_watcher_background_loop_runs_and_stops_cleanly(fake_db, fake_gemini):
    """Vòng lặp nền `run_forever()` tự phát hiện thay đổi trên Atlas và dừng sạch khi `is_closed()` trả True."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-watcher-loop",
    )
    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    await container.sync_coordinator.attach_db(fake_db)

    watcher = container.atlas_config_watcher
    watcher.poll_interval_seconds = 0.05

    closed = False
    task = asyncio.create_task(watcher.run_forever(lambda: closed))

    try:
        remote_doc = await fake_db["bot_config"].find_one({"_id": "global"})
        remote_doc["ask"]["user_cooldown"] = 12.0
        await fake_db["bot_config"].replace_one({"_id": "global"}, remote_doc, upsert=True)

        # Chờ tối đa 1 giây cho vòng lặp nền tự đồng bộ
        for _ in range(25):
            if container.config_repo.ask_cfg().get("user_cooldown") == 12.0:
                break
            await asyncio.sleep(0.04)

        assert container.config_repo.ask_cfg()["user_cooldown"] == 12.0
        assert watcher.status_snapshot()["sync_count"] >= 1
    finally:
        closed = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
