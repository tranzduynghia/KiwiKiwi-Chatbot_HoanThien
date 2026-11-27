"""Kiểm thử toàn diện WARP User-Space Proxy (wgcf + wireproxy, 127.0.0.1:40000), fallback Direct, API toggle/trace và vệ sinh HTML comment."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.bot.client import AskBot
from app.core.security import is_sensitive_key, redact_secrets, sanitize_config_for_response
from app.core.settings import AppSettings
from app.core.warp_proxy import (
    WarpProxyService,
    parse_trace_payload,
    parse_wgcf_profile,
    render_wireproxy_config,
    sanitize_warp_diagnostic,
)
from app.main import build_container, build_gemini_http_options
from app.web.app import create_dashboard_app


def test_dockerfile_and_entrypoint_install_static_wgcf_wireproxy():
    """Xác minh Dockerfile cài binary tĩnh wgcf + wireproxy ở stage cuối, giữ nguyên cloudflared, và entrypoint bind 127.0.0.1 non-blocking."""
    root = Path(__file__).resolve().parents[1]
    dockerfiles = sorted(p.name for p in root.glob("Dockerfile*"))
    assert dockerfiles == ["Dockerfile"]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (root / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "FROM python:3.12-slim-bookworm" in dockerfile
    assert dockerfile.count("FROM ") == 1
    assert "ARG TARGETARCH" in dockerfile
    assert "github.com/ViRb3/wgcf/releases/download" in dockerfile
    assert "github.com/whyvl/wireproxy/releases/download" in dockerfile
    assert "/usr/local/bin/wgcf" in dockerfile and "/usr/local/bin/wireproxy" in dockerfile
    assert "RUN wgcf --help >/dev/null && wireproxy --version" in dockerfile
    assert "/var/lib/warp-proxy" in dockerfile
    assert "cloudflare-warp" not in dockerfile
    assert "pkg.cloudflareclient.com" not in dockerfile
    assert "COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared" in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile

    assert "warp-cli" not in entrypoint and "warp-svc" not in entrypoint
    assert "register --accept-tos" in entrypoint
    assert "run_wgcf generate" in entrypoint
    assert "WGCF_LICENSE_KEY=" in entrypoint
    assert 'BindAddress = 127.0.0.1:{socks_port}' in entrypoint
    assert "BindAddress = 0.0.0.0" not in entrypoint
    assert 'wireproxy -c "${WARP_STATE_DIR}/wireproxy.conf"' in entrypoint

    # Chạy thật docker-entrypoint.sh khi không có wgcf/wireproxy -> phải khởi chạy lệnh Python bình thường không bị block hay crash
    proc = subprocess.run(
        [str(root / "docker-entrypoint.sh"), "python3", "-c", "print('warp-fallback-boot-ok')"],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PATH": "/usr/bin:/bin", "WARP_ENABLED": "true", "WARP_STARTUP_TIMEOUT": "2"},
    )
    assert proc.returncode == 0
    assert "warp-fallback-boot-ok" in proc.stdout


def test_wireproxy_config_rendering_binds_loopback_only():
    """`wireproxy.conf` sinh từ `wgcf-profile.conf` phải giữ khóa WireGuard và chỉ bind `127.0.0.1:<port>`."""
    interface, peer = parse_wgcf_profile(_FAKE_PROFILE)
    conf = render_wireproxy_config(interface, peer, socks_port=40000, http_port=40001)
    assert "[Interface]" in conf and "[Peer]" in conf
    assert "PrivateKey = " + _FAKE_PRIVATE_KEY in conf
    assert "Endpoint = engage.cloudflareclient.com:2408" in conf
    assert "[Socks5]\nBindAddress = 127.0.0.1:40000" in conf
    assert "[http]\nBindAddress = 127.0.0.1:40001" in conf
    assert "0.0.0.0:" not in conf

    with pytest.raises(ValueError):
        parse_wgcf_profile("[Interface]\nAddress = 172.16.0.2/32\n")

    trace = parse_trace_payload("fl=1\nip=203.0.113.77\ncolo=SIN\nloc=VN\nhttp=http/1.1\nwarp=plus\n")
    assert trace == {"warp": "plus", "warp_ok": True, "colo": "SIN", "loc": "VN", "http": "http/1.1"}
    assert "203.0.113.77" not in str(trace)


@pytest.mark.asyncio
async def test_warp_proxy_routes_gemini_and_discord_with_graceful_fallback():
    """Khi 127.0.0.1:<port> mở -> Gemini & Discord đi qua WARP (socks5h:// chống rò DNS); khi tắt/mất kết nối -> fallback Direct, MongoDB giữ TLS trực tiếp."""
    # Mở một cổng TCP loopback tạm thời để mô phỏng WARP proxy 127.0.0.1:40000 đang lắng nghe
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    listen_port = server.sockets[0].getsockname()[1]

    created_clients: list[tuple[str | None, Any]] = []

    def fake_gemini_factory(proxy_url: str | None):
        client = SimpleNamespace(proxy_url=proxy_url, closed=False)
        created_clients.append((proxy_url, client))
        return client

    try:
        settings = AppSettings(
            discord_token="test-token",
            gemini_api_key="test-key",
            mongodb_uri="mongodb+srv://user:pass@cluster0.example.mongodb.net/?retryWrites=true&w=majority",
            session_secret="test-secret-warp-routing-check-0123456789",
            warp_proxy_port=listen_port,
        )
        warp_service = WarpProxyService(
            warp_port=listen_port,
            enabled=True,
            metrics_fn=lambda: (7788, 29.4),
            cache_ttl=0.05,
        )
        warp_service.probe_initial_sync()

        container = build_container(
            settings,
            gemini_client_factory=fake_gemini_factory,
            reserve_buffer_bytes=16 * 1024,
            warp_proxy=warp_service,
        )
        bot = AskBot(container)

        expected_proxy = f"socks5h://127.0.0.1:{listen_port}"
        assert warp_service.is_active is True
        assert created_clients[-1][0] == expected_proxy
        assert bot._active_outbound_proxy == expected_proxy
        # MongoDB Atlas vẫn giữ URI TLS trực tiếp, không bị sửa sang proxy
        assert container.db_manager.uri == settings.mongodb_uri

        # Kiểm tra build_gemini_http_options với proxy động socks5h:// và Direct
        opts_warp = build_gemini_http_options(settings, proxy_url=expected_proxy)
        assert opts_warp is not None
        assert opts_warp.client_args == {"proxy": expected_proxy}
        assert opts_warp.async_client_args == {"proxy": expected_proxy}
        assert build_gemini_http_options(settings, proxy_url="") is None

        # Tắt nóng WARP Outbound -> Gemini client & Discord lập tức chuyển về Direct (None)
        snap_off = await warp_service.set_enabled(False, force_probe=True)
        assert snap_off["enabled"] is False
        assert snap_off["active"] is False
        assert snap_off["outbound_mode"] == "direct"
        assert created_clients[-1][0] is None
        assert bot._active_outbound_proxy is None
        assert bot.http.proxy is None

        # Bật lại nóng WARP Outbound -> khôi phục định tuyến qua socks5h://127.0.0.1:<port>
        snap_on = await warp_service.set_enabled(True, force_probe=True)
        assert snap_on["active"] is True
        assert snap_on["outbound_mode"] == "proxy"
        assert snap_on["warp_ram_mb"] == 29.4
        assert snap_on["warp_pid"] == 7788
        assert created_clients[-1][0] == expected_proxy
        assert bot._active_outbound_proxy == expected_proxy

        # Đóng cổng giả lập WARP -> refresh_status tự động fallback về Direct mà không lỗi
        server.close()
        await server.wait_closed()
        snap_fallback = await warp_service.refresh_status(force=True)
        assert snap_fallback["connected"] is False
        assert snap_fallback["active"] is False
        assert snap_fallback["fallback_direct"] is True
        assert snap_fallback["outbound_mode"] == "direct"
        assert created_clients[-1][0] is None
        assert bot._active_outbound_proxy is None
        assert bot.http.proxy is None
    finally:
        server.close()


@pytest.mark.asyncio
async def test_gemini_mid_request_proxy_error_triggers_direct_fallback(fake_db):
    """Nếu đang gọi Gemini qua WARP mà proxy đột ngột ngắt kết nối -> GeminiChatService tự fallback Direct và thử lại thành công."""
    call_log: list[str | None] = []

    class ProxyAwareFakeGemini:
        def __init__(self, proxy_url: str | None):
            self.proxy_url = proxy_url
            self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate_content))

        async def _generate_content(self, **_kwargs):
            call_log.append(self.proxy_url)
            if self.proxy_url is not None:
                raise ConnectionRefusedError("Proxy 127.0.0.1:40000 refused connection")
            return SimpleNamespace(text="Trả lời thành công qua Direct fallback")

    warp_service = WarpProxyService(
        warp_port=40000,
        enabled=True,
        probe_sync_fn=lambda _h, _p, _t: True,
        probe_async_fn=lambda _h, _p, _t: asyncio.sleep(0, result=True),
        metrics_fn=lambda: (9001, 31.2),
    )
    warp_service.probe_initial_sync()

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-mid-request-fallback-1234567890",
    )
    container = build_container(
        settings,
        gemini_client_factory=lambda proxy_url: ProxyAwareFakeGemini(proxy_url),
        reserve_buffer_bytes=16 * 1024,
        warp_proxy=warp_service,
    )
    await container.sync_coordinator.attach_db(fake_db)

    resp, _thinking = await container.gemini_service.call_model(
        "gemini-2.5-flash",
        ["Xin chào"],
        system="Bạn là trợ lý.",
        thinking_level=None,
        web_search=False,
    )
    assert resp.text == "Trả lời thành công qua Direct fallback"
    assert call_log == ["socks5h://127.0.0.1:40000", None]
    assert warp_service.is_active is False
    assert warp_service.status_snapshot()["fallback_direct"] is True


@pytest.mark.asyncio
async def test_warp_dashboard_api_toggle_rbac_status_and_zero_html_comments(fake_db, fake_gemini, fake_bot):
    """Kiểm thử PUT /api/config/warp (Admin-only, Audit Log, telemetry /api/status) và xác minh 0 comment HTML trên Dashboard/Login."""
    warp_state = {"reachable": True}

    async def fake_probe_async(_host: str, _port: int, _timeout: float) -> bool:
        return warp_state["reachable"]

    warp_service = WarpProxyService(
        warp_port=40000,
        enabled=True,
        probe_sync_fn=lambda _h, _p, _t: warp_state["reachable"],
        probe_async_fn=fake_probe_async,
        metrics_fn=lambda: (6543, 27.8) if warp_state["reachable"] else (None, None),
        cache_ttl=0.01,
    )
    warp_service.probe_initial_sync()

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-warp-dashboard-api-0123456789",
    )
    container = build_container(
        settings,
        gemini_client=fake_gemini,
        reserve_buffer_bytes=16 * 1024,
        warp_proxy=warp_service,
    )
    await container.sync_coordinator.attach_db(fake_db)
    await container.session_repo.attach_db(fake_db)
    await container.model_registry.refresh()
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await container.auth_service.create_user("operator1", "OperPass123!", role="operator")

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        # 1) Trang /login không có bất kỳ HTML comment nào xuất hiện trên DevTools
        login_html = client.get("/login")
        assert login_html.status_code == 200
        assert "<!--" not in login_html.text

        # 1b) Chưa đăng nhập -> bị chặn 401 khi gọi PUT /api/config/warp
        assert client.put("/api/config/warp", json={"warp_outbound_enabled": False}).status_code == 401

        # 2) Operator bị chặn 403 khi cố bật/tắt WARP Outbound
        client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        assert client.put("/api/config/warp", json={"warp_outbound_enabled": False}).status_code == 403

        # 3) Admin đăng nhập -> kiểm tra /dashboard có đủ phần tử WARP UI và 0 HTML comments
        client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        dash_html = client.get("/dashboard")
        assert dash_html.status_code == 200
        assert "<!--" not in dash_html.text
        assert 'id="cf-outbound-badge"' in dash_html.text
        assert 'id="cf-outbound-dot"' in dash_html.text
        assert 'id="cf-warp-outbound-toggle"' in dash_html.text
        assert 'id="cf-warp-ram-badge"' in dash_html.text

        # 4) /api/status báo Cloudflare WARP đang kết nối kèm RAM
        st_active = client.get("/api/status").json()["cloudflare_tunnel"]
        assert st_active["outbound_mode"] == "proxy"
        assert st_active["warp_active"] is True
        assert st_active["warp_connected"] is True
        assert st_active["warp_ram_mb"] == 27.8
        assert st_active["warp_pid"] == 6543

        # 5) Admin tắt WARP Outbound qua PUT /api/config/warp -> chuyển ngay sang Direct & ghi Audit Log
        res_off = client.put("/api/config/warp", json={"warp_outbound_enabled": False})
        assert res_off.status_code == 200
        body_off = res_off.json()
        assert body_off["ok"] is True
        assert body_off["warp"]["active"] is False
        assert body_off["cloudflare_tunnel"]["outbound_mode"] == "direct"
        assert body_off["config"]["tunnel"]["warp_outbound_enabled"] is False

        # 6) Admin bật lại WARP Outbound -> khôi phục ngay Outbound: Cloudflare WARP
        res_on = client.put("/api/config/warp", json={"warp_outbound_enabled": True})
        assert res_on.status_code == 200
        assert res_on.json()["warp"]["active"] is True
        assert res_on.json()["cloudflare_tunnel"]["outbound_mode"] == "proxy"

        # 7) Kiểm tra Audit Log đã ghi lại thao tác chuyển đổi WARP trong section="tunnel"
        audit_items = client.get("/api/audit-logs?section=tunnel").json()["items"]
        assert len(audit_items) >= 2
        assert any("WARP" in str(item.get("summary", "")) for item in audit_items)


def test_warp_license_key_zero_secret_protection():
    """Đảm bảo WARP_LICENSE_KEY luôn bị nhận diện là trường bí mật và bị loại bỏ khỏi API/log."""
    assert is_sensitive_key("warp_license_key") is True
    assert is_sensitive_key("tunnel.warp_outbound_enabled") is False
    assert "[REDACTED]" in redact_secrets("license_key=abc123-super-secret-warp-plus")

    sanitized = sanitize_config_for_response({
        "tunnel": {
            "warp_outbound_enabled": True,
            "warp_license_key": "SUPER-SECRET-WARP-PLUS-KEY",
        }
    })
    assert sanitized["tunnel"]["warp_outbound_enabled"] is True
    assert "warp_license_key" not in sanitized["tunnel"]


_FAKE_PRIVATE_KEY = "kOc3Zr1Lq3lYpPq0p2Vx7mYc3e9bHq2f3T0dY0xg2Xo="
_FAKE_PROFILE = f"""[Interface]
PrivateKey = {_FAKE_PRIVATE_KEY}
Address = 172.16.0.2/32, 2606:4700:110:8176:3744:8216:1861:296a/128
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280
[Peer]
PublicKey = bmXOC+F1FxEMF9dyiK2H5/1SUtzH0JuVo51h2wPfgyo=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = engage.cloudflareclient.com:2408
"""


class _FakeCliProcess:
    """Mô phỏng tiến trình `wgcf` trả về stdout/stderr cho `communicate()`."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


class _FakeDaemonProcess:
    """Mô phỏng tiến trình nền `wireproxy` có thể sống bền hoặc thoát sớm."""

    def __init__(self, pid: int = 8800, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


@pytest.mark.asyncio
async def test_warp_start_update_stop_trace_wgcf_wireproxy_lifecycle(
    fake_db, fake_gemini, fake_bot, monkeypatch, tmp_path
):
    """POST /api/warp/start|update|stop + /api/warp/trace: wgcf register/update/generate, sinh wireproxy.conf, spawn wireproxy, poll socket và trace."""
    monkeypatch.setenv("WARP_LICENSE_KEY", "SUPER-SECRET-WARP-KEY-9999")
    monkeypatch.delenv("WGCF_LICENSE_KEY", raising=False)
    state_dir = tmp_path / "warp-proxy"

    state: dict[str, Any] = {
        "wgcf_calls": [],
        "child_envs": {},
        "spawned": [],
        "listening_port": None,
        "polls": 0,
        "external_terminations": 0,
    }

    async def fake_spawn(*cmd, **kwargs):
        binary = Path(cmd[0]).name
        env = dict(kwargs.get("env") or {})
        cwd = Path(kwargs.get("cwd") or ".")
        if binary == "wgcf":
            sub = cmd[1]
            state["wgcf_calls"].append(tuple(cmd[1:]))
            state["child_envs"][sub] = env
            if sub == "register":
                assert "--accept-tos" in cmd
                (cwd / "wgcf-account.toml").write_text('private_key = "x"\n', encoding="utf-8")
            elif sub == "update":
                assert env.get("WGCF_LICENSE_KEY") == "SUPER-SECRET-WARP-KEY-9999"
            elif sub == "generate":
                (cwd / "wgcf-profile.conf").write_text(_FAKE_PROFILE, encoding="utf-8")
            return _FakeCliProcess(0, stdout=b"ok")
        assert binary == "wireproxy"
        assert cmd[1:] == ("-c", str(state_dir / "wireproxy.conf"))
        state["child_envs"]["wireproxy"] = env
        conf = (state_dir / "wireproxy.conf").read_text(encoding="utf-8")
        port = int(conf.split("[Socks5]\nBindAddress = 127.0.0.1:")[1].split()[0])
        state["listening_port"] = port
        state["polls"] = 0
        proc = _FakeDaemonProcess(pid=8811 + len(state["spawned"]))
        state["spawned"].append(proc)
        return proc

    async def fake_probe_async(host: str, port: int, _timeout: float) -> bool:
        assert host == "127.0.0.1"
        alive = state["spawned"] and state["spawned"][-1].returncode is None
        if not alive or port != state["listening_port"]:
            return False
        state["polls"] += 1
        return state["polls"] >= 3

    async def fake_trace(proxy_url: str) -> str:
        assert proxy_url.startswith("socks5h://127.0.0.1:")
        return "fl=1\nip=198.51.100.23\nts=1\ncolo=SIN\nloc=VN\nwarp=plus\n"

    def fake_terminate_external(_keep: set[int]) -> int:
        state["external_terminations"] += 1
        return 0

    warp_service = WarpProxyService(
        warp_port=40000,
        enabled=False,
        probe_sync_fn=lambda _h, _p, _t: False,
        probe_async_fn=fake_probe_async,
        metrics_fn=lambda: (8811, 10.4) if state["spawned"] and state["spawned"][-1].returncode is None else (None, None),
        which_fn=lambda name: f"/usr/local/bin/{name}" if name in ("wgcf", "wireproxy") else None,
        spawn_subprocess=fake_spawn,
        trace_fn=fake_trace,
        terminate_external_fn=fake_terminate_external,
        state_dir=str(state_dir),
        connect_poll_timeout=1.0,
        connect_poll_interval=0.02,
    )

    settings = AppSettings(
        discord_token="test-token",
        gemini_api_key="test-key",
        session_secret="test-secret-warp-lifecycle-0123456789",
    )
    container = build_container(
        settings,
        gemini_client=fake_gemini,
        reserve_buffer_bytes=16 * 1024,
        warp_proxy=warp_service,
    )
    await container.sync_coordinator.attach_db(fake_db)
    await container.session_repo.attach_db(fake_db)
    await container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    await container.auth_service.create_user("operator1", "OperPass123!", role="operator")

    web_app = create_dashboard_app(container, get_bot=lambda: fake_bot)
    with TestClient(web_app) as client:
        for path in ("/api/warp/start", "/api/warp/update", "/api/warp/stop", "/api/warp/trace"):
            assert client.post(path).status_code == 401

        client.post("/auth/login", json={"username": "operator1", "password": "OperPass123!"})
        for path in ("/api/warp/start", "/api/warp/update", "/api/warp/stop", "/api/warp/trace"):
            assert client.post(path).status_code == 403
        assert client.get("/api/warp/trace").status_code == 403

        client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"})
        start_res = client.post("/api/warp/start", json={"warp_outbound_enabled": True})
        assert start_res.status_code == 200
        start_body = start_res.json()
        assert start_body["warp"]["active"] is True
        assert start_body["warp"]["engine"] == "wireproxy"
        assert start_body["warp"]["warp_installed"] is True
        assert start_body["warp"]["diagnostic"] is None
        assert start_body["warp"]["proxy_url"] == "socks5h://127.0.0.1:40000"
        assert start_body["warp"]["trace"]["warp"] == "plus"
        assert start_body["warp"]["trace"]["warp_ok"] is True
        assert start_body["cloudflare_tunnel"]["outbound_mode"] == "proxy"
        assert [c[0] for c in state["wgcf_calls"]] == ["register", "update", "generate"]
        assert state["polls"] >= 3

        conf_path = state_dir / "wireproxy.conf"
        conf = conf_path.read_text(encoding="utf-8")
        assert "BindAddress = 127.0.0.1:40000" in conf
        assert "0.0.0.0:" not in conf
        assert (conf_path.stat().st_mode & 0o077) == 0
        assert "SUPER-SECRET" not in (state_dir / ".license-applied").read_text(encoding="utf-8")

        # Zero-secret: không lộ license key, private key hay IP thô trong JSON trả về
        for leaked in ("SUPER-SECRET-WARP-KEY-9999", _FAKE_PRIVATE_KEY, "198.51.100.23"):
            assert leaked not in start_res.text
        # License chỉ được truyền cho `wgcf update`; không tiến trình con nào nhận bí mật bot
        assert "WGCF_LICENSE_KEY" not in state["child_envs"]["register"]
        assert "WGCF_LICENSE_KEY" not in state["child_envs"]["generate"]
        assert "WGCF_LICENSE_KEY" not in state["child_envs"]["wireproxy"]
        assert all("WARP_LICENSE_KEY" not in env for env in state["child_envs"].values())
        assert all("DISCORD_TOKEN" not in env for env in state["child_envs"].values())

        trace_res = client.get("/api/warp/trace")
        assert trace_res.status_code == 200
        assert trace_res.json()["ok"] is True
        assert trace_res.json()["trace"]["colo"] == "SIN"
        assert "198.51.100.23" not in trace_res.text

        # Cập nhật cổng 40001 -> sinh lại wireproxy.conf và khởi động lại wireproxy (không đăng ký lại wgcf)
        upd_res = client.post("/api/warp/update", json={"warp_outbound_enabled": True, "warp_proxy_port": 40001})
        assert upd_res.status_code == 200
        assert upd_res.json()["warp"]["active"] is True
        assert upd_res.json()["warp"]["proxy_url"] == "socks5h://127.0.0.1:40001"
        assert "BindAddress = 127.0.0.1:40001" in conf_path.read_text(encoding="utf-8")
        assert len(state["spawned"]) == 2
        assert state["spawned"][0].terminated is True
        assert [c[0] for c in state["wgcf_calls"]] == ["register", "update", "generate"]

        stop_res = client.post("/api/warp/stop")
        assert stop_res.status_code == 200
        assert stop_res.json()["warp"]["active"] is False
        assert stop_res.json()["cloudflare_tunnel"]["outbound_mode"] == "direct"
        assert state["spawned"][-1].terminated is True
        assert state["external_terminations"] >= 1


@pytest.mark.asyncio
async def test_warp_clear_sanitized_diagnostics_on_missing_binary_and_wireproxy_failure(monkeypatch, tmp_path):
    """Chẩn đoán rõ ràng & đã che bí mật/khóa/IP/đường dẫn khi thiếu binary, wgcf lỗi hoặc wireproxy thoát sớm."""
    monkeypatch.setenv("WARP_LICENSE_KEY", "SECRET-KEY-SHOULD-NOT-LEAK")

    # 1) Thiếu binary -> "Chưa cài wgcf/wireproxy" (không còn báo "Chưa cài cloudflare-warp")
    missing_service = WarpProxyService(
        warp_port=40000,
        probe_sync_fn=lambda _h, _p, _t: False,
        probe_async_fn=lambda _h, _p, _t: asyncio.sleep(0, result=False),
        which_fn=lambda _name: None,
        state_dir=str(tmp_path / "missing"),
    )
    snap_missing = await missing_service.start_and_connect()
    assert snap_missing["active"] is False
    assert snap_missing["warp_installed"] is False
    assert snap_missing["diagnostic_code"] == "not_installed"
    assert snap_missing["diagnostic"] == "Chưa cài wgcf/wireproxy"
    assert "cloudflare-warp" not in str(snap_missing)

    only_wgcf = WarpProxyService(which_fn=lambda name: "/usr/local/bin/wgcf" if name == "wgcf" else None)
    assert only_wgcf.is_installed() is False
    assert only_wgcf._not_installed_message() == "Chưa cài wireproxy"

    # 2) wgcf register thất bại -> chẩn đoán đã che license + IP
    async def failing_register(*cmd, **kwargs):
        return _FakeCliProcess(
            1,
            stderr=b"Error: failed to register: dial tcp 162.159.192.1:443: connect: connection refused SECRET-KEY-SHOULD-NOT-LEAK",
        )

    reg_fail = WarpProxyService(
        probe_async_fn=lambda _h, _p, _t: asyncio.sleep(0, result=False),
        metrics_fn=lambda: (None, None),
        which_fn=lambda name: f"/usr/local/bin/{name}",
        spawn_subprocess=failing_register,
        terminate_external_fn=lambda _k: 0,
        state_dir=str(tmp_path / "regfail"),
    )
    snap_reg = await reg_fail.start_and_connect()
    assert snap_reg["diagnostic_code"] == "registration_failed"
    assert snap_reg["diagnostic"].startswith("Đăng ký tài khoản WARP thất bại")
    assert "SECRET-KEY-SHOULD-NOT-LEAK" not in snap_reg["diagnostic"]
    assert "162.159.192.1" not in snap_reg["diagnostic"]

    # 3) wireproxy thoát sớm với log chứa đường dẫn nội bộ, private key, IP endpoint -> phải che sạch
    state_dir = tmp_path / "wpfail"
    state_dir.mkdir()
    (state_dir / "wgcf-account.toml").write_text("x = 1\n", encoding="utf-8")
    (state_dir / "wgcf-profile.conf").write_text(_FAKE_PROFILE, encoding="utf-8")
    (state_dir / ".license-applied").write_text(
        __import__("hashlib").sha256(b"SECRET-KEY-SHOULD-NOT-LEAK").hexdigest(), encoding="utf-8"
    )

    async def failing_spawn(*cmd, **kwargs):
        assert Path(cmd[0]).name == "wireproxy"
        (state_dir / "wireproxy.log").write_text(
            "DEBUG: noisy handshake\n"
            f"ERROR: listen tcp 127.0.0.1:40000: bind: address already in use /var/lib/warp-proxy/wireproxy.conf "
            f"PrivateKey={_FAKE_PRIVATE_KEY} peer 162.159.192.1:2408\n",
            encoding="utf-8",
        )
        return _FakeDaemonProcess(pid=9901, returncode=1)

    failing_service = WarpProxyService(
        warp_port=40000,
        probe_sync_fn=lambda _h, _p, _t: False,
        probe_async_fn=lambda _h, _p, _t: asyncio.sleep(0, result=False),
        metrics_fn=lambda: (None, None),
        which_fn=lambda name: f"/usr/local/bin/{name}",
        spawn_subprocess=failing_spawn,
        terminate_external_fn=lambda _k: 0,
        state_dir=str(state_dir),
        connect_poll_timeout=0.3,
        connect_poll_interval=0.02,
    )
    snap_fail = await failing_service.start_and_connect()
    assert snap_fail["active"] is False
    assert snap_fail["diagnostic_code"] == "daemon_not_running"
    diag = snap_fail["diagnostic"]
    assert diag.startswith("wireproxy thoát sớm")
    assert "address already in use" in diag
    assert "127.0.0.1:40000" in diag
    for leaked in ("SECRET-KEY-SHOULD-NOT-LEAK", _FAKE_PRIVATE_KEY, "162.159.192.1", "/var/lib/warp-proxy"):
        assert leaked not in diag

    assert sanitize_warp_diagnostic("peer 2606:4700:d0::a29f:c001 timeout at 15:00:31") == "peer [ip] timeout at 15:00:31"
