"""Kiểm thử toàn diện tích hợp Cloudflare Tunnel (cloudflared), khóa Inbound, Outbound Proxy và cgroup Watchdog."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bot.client import AskBot
from app.core.cloudflare_tunnel import CloudflareTunnelService
from app.core.memory_watchdog import read_container_memory_bytes
from app.core.settings import AppSettings
from app.main import build_container, build_gemini_http_options
from app.web.app import create_dashboard_app


class FakeStreamReader:
    """Mô phỏng `asyncio.StreamReader` trả về từng dòng stderr của tiến trình `cloudflared`."""

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
    """Mô phỏng tiến trình con bất đồng bộ `cloudflared` hỗ trợ `terminate`, `kill`, `wait` và `stderr`."""

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


def test_dockerfile_bundles_official_cloudflared_binary():
    """Xác minh Dockerfile sao chép binary cloudflared chính thức từ cloudflare/cloudflared:latest."""
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared" in dockerfile


def test_memory_watchdog_reads_cgroup_container_memory(tmp_path, monkeypatch):
    """MemoryWatchdog ưu tiên đọc tổng RAM container từ cgroup (gồm Python + cloudflared) trước khi fallback RSS."""
    fake_cgroup = tmp_path / "memory.current"
    fake_cgroup.write_text("412000000\n", encoding="utf-8")
    monkeypatch.setattr("app.core.memory_watchdog.CGROUP_MEMORY_PATHS", (str(fake_cgroup),))
    assert read_container_memory_bytes() == 412_000_000

    # Khi không có cgroup file -> tự fallback sang psutil Process RSS (> 0)
    monkeypatch.setattr("app.core.memory_watchdog.CGROUP_MEMORY_PATHS", (str(tmp_path / "missing"),))
    assert read_container_memory_bytes() > 0


@pytest.mark.asyncio
async def test_cloudflare_tunnel_service_named_quick_and_missing_binary():
    """Kiểm tra Named Tunnel CLI, Quick Tunnel bắt URL trycloudflare.com từ stderr, tắt an toàn và thiếu binary."""
    spawned_commands: list[tuple[str, ...]] = []
    spawned_envs: list[dict[str, str]] = []
    created_procs: list[FakeSubprocess] = []

    async def fake_spawn(*cmd, **kwargs):
        spawned_commands.append(cmd)
        spawned_envs.append(dict(kwargs.get("env") or {}))
        stderr_lines = (
            [
                b"INF Starting Quick Tunnel...\n",
                b"INF Your quick Tunnel has been created! Visit it at: https://kiwi-fast-demo.trycloudflare.com\n",
            ]
            if "--url" in cmd
            else [b"INF Registered tunnel connection\n"]
        )
        proc = FakeSubprocess(pid=4200 + len(created_procs), stderr_lines=stderr_lines)
        created_procs.append(proc)
        return proc

    service = CloudflareTunnelService(
        local_port=10000,
        outbound_proxy_url="http://127.0.0.1:40000",
        spawn_subprocess=fake_spawn,
        quick_url_wait_seconds=1.0,
    )

    # 1) Named Tunnel Mode (token cấp qua biến môi trường TUNNEL_TOKEN, không lộ trên argv)
    named_status = await service.apply_config({
        "cf_tunnel_enabled": True,
        "cf_tunnel_mode": "named",
        "cf_tunnel_token": "eyJ-test-token-123",
        "tunnel_only_inbound": False,
    })
    assert named_status["running"] is True
    assert named_status["mode"] == "named"
    assert named_status["pid"] == 4200
    assert named_status["outbound_mode"] == "proxy"
    assert spawned_commands[-1] == (
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--edge-ip-version",
        "auto",
        "run",
    )
    assert "--token" not in spawned_commands[-1]
    assert spawned_envs[-1]["TUNNEL_TOKEN"] == "eyJ-test-token-123"

    # 2) Hot-reload sang Quick Tunnel Mode -> tự tắt tiến trình cũ và bắt URL trycloudflare.com
    quick_status = await service.apply_config({
        "cf_tunnel_enabled": True,
        "cf_tunnel_mode": "quick",
        "cf_tunnel_token": "",
        "tunnel_only_inbound": False,
    })
    assert created_procs[0].terminated is True
    assert quick_status["running"] is True
    assert quick_status["mode"] == "quick"
    assert quick_status["public_url"] == "https://kiwi-fast-demo.trycloudflare.com"
    assert spawned_commands[-1] == (
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:10000",
    )

    # 3) Dừng an toàn
    stopped = await service.stop()
    assert created_procs[1].terminated is True
    assert stopped["running"] is False
    assert stopped["public_url"] == ""

    # 4) Xử lý thiếu binary không làm crash ứng dụng
    async def missing_spawn(*_cmd, **_kwargs):
        raise FileNotFoundError("cloudflared")

    missing_service = CloudflareTunnelService(spawn_subprocess=missing_spawn)
    err_status = await missing_service.apply_config({
        "cf_tunnel_enabled": True,
        "cf_tunnel_mode": "quick",
        "cf_tunnel_token": "",
        "tunnel_only_inbound": False,
    })
    assert err_status["running"] is False
    assert err_status["last_error"] is not None
    assert "cloudflared" in err_status["last_error"]


def test_outbound_proxy_url_propagates_to_gemini_and_discord_bot(fake_gemini):
    """OUTBOUND_PROXY_URL được truyền vào Gemini HttpOptions và discord.Client mà vẫn giữ MongoDB trực tiếp."""
    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret",
        outbound_proxy_url="http://127.0.0.1:40000",
    )
    http_opts = build_gemini_http_options(settings)
    assert http_opts is not None
    assert http_opts.client_args == {"proxy": "http://127.0.0.1:40000"}
    assert http_opts.async_client_args == {"proxy": "http://127.0.0.1:40000"}

    container = build_container(settings, gemini_client=fake_gemini, reserve_buffer_bytes=16 * 1024)
    bot = AskBot(container)
    assert bot.http.proxy == "http://127.0.0.1:40000"
    assert container.cloudflare_tunnel.status_snapshot()["outbound_mode"] == "proxy"


@pytest.mark.asyncio
async def test_tunnel_api_hot_reload_token_masking_audit_and_inbound_lock(fake_db, fake_gemini, fake_bot):
    """Kiểm thử API PUT /api/config/tunnel, ẩn token, ghi Audit Log và middleware chặn truy cập trực tiếp."""
    spawned: list[tuple[str, ...]] = []

    async def fake_spawn(*cmd, **_kwargs):
        spawned.append(cmd)
        lines = (
            [b"INF https://hot-reload-check.trycloudflare.com\n"]
            if "--url" in cmd
            else [b"INF Connected to Cloudflare Edge\n"]
        )
        return FakeSubprocess(pid=5100 + len(spawned), stderr_lines=lines)

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-for-tunnel-tests",
        port=10000,
    )
    tunnel_service = CloudflareTunnelService(
        local_port=10000,
        spawn_subprocess=fake_spawn,
        quick_url_wait_seconds=1.0,
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
    await container.auth_service.create_user("operator1", "OperPass123!", role="operator")

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        # Operator bị chặn 403 khi chỉnh Tunnel
        client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        assert client.put(
            "/api/config/tunnel",
            json={"cf_tunnel_enabled": True, "cf_tunnel_mode": "quick", "tunnel_only_inbound": False},
        ).status_code == 403

        # Admin đăng nhập và kiểm tra giao diện thẻ Cloudflare Tunnel (2 nút xếp dọc + trạng thái tắt ban đầu + không rò comment HTML)
        client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        dash = client.get("/dashboard")
        assert dash.status_code == 200
        assert 'id="cf-tunnel-mode"' in dash.text
        assert 'id="cf-tunnel-token"' in dash.text
        assert 'id="btn-toggle-tunnel"' in dash.text
        assert 'data-tunnel-active="false"' in dash.text
        assert "bg-emerald-600" in dash.text
        assert 'id="btn-save-tunnel"' in dash.text
        assert "Bật Tunnel" in dash.text
        assert "Cập nhật Tunnel" in dash.text
        assert "Origin nội bộ:" not in dash.text
        assert "<!--" not in dash.text

        # Bật Named Tunnel -> lưu token trong MongoDB nhưng ẩn trong API response & Audit Log
        secret_token = "eyJ-super-secret-cf-token-xyz"
        named_res = client.put(
            "/api/config/tunnel",
            json={
                "cf_tunnel_enabled": True,
                "cf_tunnel_mode": "named",
                "cf_tunnel_token": secret_token,
                "tunnel_only_inbound": False,
            },
        )
        assert named_res.status_code == 200
        named_json = named_res.json()
        assert "cf_tunnel_token" not in named_json["config"]["tunnel"]
        assert named_json["config"]["tunnel"]["has_cf_tunnel_token"] is True
        assert named_json["config"]["tunnel"]["cf_tunnel_has_token"] is True
        assert named_json["cloudflare_tunnel"]["running"] is True
        assert container.config_repo.tunnel_cfg()["cf_tunnel_token"] == secret_token

        # Sau khi Tunnel đang chạy -> SSR /dashboard phải render nút đỏ "Tắt Tunnel" (data-tunnel-active="true")
        dash_running = client.get("/dashboard")
        assert dash_running.status_code == 200
        assert 'data-tunnel-active="true"' in dash_running.text
        assert "bg-rose-600" in dash_running.text
        assert "Tắt Tunnel" in dash_running.text

        # Kiểm tra /api/status xuất đầy đủ telemetry cloudflare_tunnel
        st_res = client.get("/api/status")
        assert st_res.status_code == 200
        assert st_res.json()["cloudflare_tunnel"]["running"] is True
        assert st_res.json()["cloudflare_tunnel"]["mode"] == "named"

        # Hot-reload sang Quick Tunnel + bật khóa Chỉ nhận truy cập qua Cloudflare Tunnel
        lock_res = client.put(
            "/api/config/tunnel",
            json={
                "cf_tunnel_enabled": True,
                "cf_tunnel_mode": "quick",
                "tunnel_only_inbound": True,
            },
        )
        assert lock_res.status_code == 200
        assert lock_res.json()["cloudflare_tunnel"]["public_url"] == "https://hot-reload-check.trycloudflare.com"

        # Truy cập trực tiếp KHÔNG có header Cloudflare -> bị chặn 403
        assert client.get("/api/status").status_code == 403
        assert client.get("/dashboard").status_code == 403

        # Endpoint /health luôn mở cho health check nội bộ của Northflank
        assert client.get("/health").status_code == 200

        # Truy cập qua Cloudflare Edge (có Cf-Ray hoặc CF-Connecting-IP) -> hợp lệ 200
        assert client.get("/api/status", headers={"Cf-Ray": "89ab123456-SIN"}).status_code == 200
        assert client.get("/api/status", headers={"CF-Connecting-IP": "203.0.113.10"}).status_code == 200

        # Kiểm tra Audit Log đã ghi lại thay đổi section="tunnel" mà KHÔNG lộ token nguyên bản
        audit_res = client.get("/api/audit-logs?section=tunnel", headers={"Cf-Ray": "89ab123456-SIN"})
        assert audit_res.status_code == 200
        audit_items = audit_res.json()["items"]
        assert len(audit_items) >= 2
        assert secret_token not in str(audit_items)
