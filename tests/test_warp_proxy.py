"""Kiểm thử toàn diện tích hợp Cloudflare WARP User-Space Proxy (127.0.0.1:40000), fallback Direct, API toggle và vệ sinh HTML comment."""
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
from app.core.warp_proxy import WarpProxyService
from app.main import build_container, build_gemini_http_options
from app.web.app import create_dashboard_app


def test_dockerfile_and_entrypoint_configure_cloudflare_warp_proxy():
    """Xác minh Dockerfile cài đặt cloudflare-warp, giữ nguyên cloudflared và docker-entrypoint.sh cấu hình proxy mode non-blocking."""
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (root / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert "cloudflare-warp" in dockerfile
    assert "COPY --from=cloudflare/cloudflared:latest /usr/local/bin/cloudflared /usr/local/bin/cloudflared" in dockerfile
    assert "/var/lib/cloudflare-warp" in dockerfile
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in dockerfile

    assert "warp-svc" in entrypoint
    assert "warp-cli --accept-tos mode proxy" in entrypoint
    assert "warp-cli --accept-tos proxy port" in entrypoint
    assert "warp-cli --accept-tos connect" in entrypoint
    assert "WARP_LICENSE_KEY" in entrypoint
    assert "WARP_CONNECTOR_TOKEN" in entrypoint

    # Chạy thật docker-entrypoint.sh khi không có warp-svc -> phải khởi chạy lệnh Python bình thường không bị block hay crash
    proc = subprocess.run(
        [str(root / "docker-entrypoint.sh"), "python3", "-c", "print('warp-fallback-boot-ok')"],
        capture_output=True,
        text=True,
        timeout=5,
        env={**os.environ, "WARP_ENABLED": "true", "WARP_STARTUP_TIMEOUT": "2"},
    )
    assert proc.returncode == 0
    assert "warp-fallback-boot-ok" in proc.stdout


@pytest.mark.asyncio
async def test_warp_proxy_routes_gemini_and_discord_with_graceful_fallback():
    """Khi 127.0.0.1:<port> mở -> Gemini & Discord đi qua WARP; khi tắt hoặc mất kết nối -> tự động fallback Direct, MongoDB giữ TLS trực tiếp."""
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

        expected_proxy = f"http://127.0.0.1:{listen_port}"
        assert warp_service.is_active is True
        assert created_clients[-1][0] == expected_proxy
        assert bot.http.proxy == expected_proxy
        # MongoDB Atlas vẫn giữ URI TLS trực tiếp, không bị sửa sang proxy
        assert container.db_manager.uri == settings.mongodb_uri

        # Kiểm tra build_gemini_http_options với proxy động và Direct
        opts_warp = build_gemini_http_options(settings, proxy_url=expected_proxy)
        assert opts_warp is not None
        assert opts_warp.client_args == {"proxy": expected_proxy}
        assert opts_warp.async_client_args == {"proxy": expected_proxy}
        assert build_gemini_http_options(settings, proxy_url="") is None

        # Tắt nóng WARP Outbound -> Gemini client & Discord http.proxy lập tức chuyển về Direct (None)
        snap_off = await warp_service.set_enabled(False, force_probe=True)
        assert snap_off["enabled"] is False
        assert snap_off["active"] is False
        assert snap_off["outbound_mode"] == "direct"
        assert created_clients[-1][0] is None
        assert bot.http.proxy is None

        # Bật lại nóng WARP Outbound -> khôi phục định tuyến qua 127.0.0.1:<port>
        snap_on = await warp_service.set_enabled(True, force_probe=True)
        assert snap_on["active"] is True
        assert snap_on["outbound_mode"] == "proxy"
        assert snap_on["warp_ram_mb"] == 29.4
        assert snap_on["warp_pid"] == 7788
        assert created_clients[-1][0] == expected_proxy
        assert bot.http.proxy == expected_proxy

        # Đóng cổng giả lập WARP -> refresh_status tự động fallback về Direct mà không lỗi
        server.close()
        await server.wait_closed()
        snap_fallback = await warp_service.refresh_status(force=True)
        assert snap_fallback["connected"] is False
        assert snap_fallback["active"] is False
        assert snap_fallback["fallback_direct"] is True
        assert snap_fallback["outbound_mode"] == "direct"
        assert created_clients[-1][0] is None
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
    assert call_log == ["http://127.0.0.1:40000", None]
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
