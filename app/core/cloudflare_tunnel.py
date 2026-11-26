"""Dịch vụ quản lý tiến trình con `cloudflared` bất đồng bộ (Named Tunnel Token & Quick Tunnel) không cần restart bot."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.core.security import redact_secrets, validate_cloudflare_tunnel_token

log = logging.getLogger("gemini-bot.tunnel")

TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
_ALLOWED_SUBPROCESS_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NO_PROXY",
    "no_proxy",
)


class CloudflareTunnelService:
    """Điều phối vòng đời tiến trình con `cloudflared` bằng `asyncio.create_subprocess_exec` và xuất telemetry trực tiếp."""

    def __init__(
        self,
        *,
        local_port: int = 10000,
        outbound_proxy_url: str = "",
        warp_proxy: Optional[Any] = None,
        binary_path: str = "cloudflared",
        spawn_subprocess: Optional[Callable[..., Awaitable[Any]]] = None,
        quick_url_wait_seconds: float = 2.0,
    ):
        port_int = int(local_port)
        if not (1 <= port_int <= 65535):
            raise ValueError("local_port phải nằm trong khoảng 1..65535.")
        self.local_port = port_int
        self.outbound_proxy_url = (outbound_proxy_url or "").strip()
        self._warp_proxy = warp_proxy
        self.binary_path = binary_path
        self._spawn = spawn_subprocess or asyncio.create_subprocess_exec
        self._quick_url_wait_seconds = max(0.05, float(quick_url_wait_seconds))

        self._lock = asyncio.Lock()
        self._process: Any = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._url_ready_event: asyncio.Event = asyncio.Event()

        self._enabled: bool = False
        self._mode: str = "named"
        self._tunnel_only_inbound: bool = False
        self._public_url: str = ""
        self._last_error: Optional[str] = None
        self._started_at: Optional[str] = None

    @property
    def origin_url(self) -> str:
        """Địa chỉ loopback nội bộ mà `cloudflared` chuyển tiếp lưu lượng vào máy chủ Uvicorn."""
        return f"http://127.0.0.1:{self.local_port}"

    @property
    def is_running(self) -> bool:
        """Kiểm tra tiến trình con `cloudflared` hiện còn đang chạy hay không."""
        return self._process is not None and getattr(self._process, "returncode", None) is None

    def build_command(self, mode: str, token: str = "") -> list[str]:
        """Xây dựng danh sách tham số CLI an toàn cho `cloudflared` (Named Tunnel đọc token từ biến môi trường `TUNNEL_TOKEN`, không lộ trên argv)."""
        clean_mode = (mode or "named").strip().lower()
        if clean_mode == "quick":
            return [
                self.binary_path,
                "tunnel",
                "--no-autoupdate",
                "--url",
                self.origin_url,
            ]
        # Kiểm tra token hợp lệ nếu người gọi truyền trực tiếp vào build_command; token thực tế được cấp qua env TUNNEL_TOKEN
        if token:
            validate_cloudflare_tunnel_token(token)
        return [
            self.binary_path,
            "tunnel",
            "--no-autoupdate",
            "--edge-ip-version",
            "auto",
            "run",
        ]

    @staticmethod
    def build_subprocess_env(mode: str, token: str = "") -> dict[str, str]:
        """Tạo môi trường tối giản cho tiến trình con `cloudflared`, loại bỏ toàn bộ bí mật của bot (DISCORD_TOKEN, GEMINI_API_KEY, MONGODB_URI...)."""
        child_env = {
            key: os.environ[key]
            for key in _ALLOWED_SUBPROCESS_ENV_KEYS
            if key in os.environ and os.environ[key]
        }
        if (mode or "named").strip().lower() != "quick" and token:
            child_env["TUNNEL_TOKEN"] = validate_cloudflare_tunnel_token(token)
        return child_env

    async def _consume_stderr(self, proc: Any, mode: str) -> None:
        """Đọc luồng stderr của `cloudflared` để bắt URL Quick Tunnel và phát hiện lỗi thoát sớm."""
        stderr = getattr(proc, "stderr", None)
        if stderr is None:
            return
        last_err_line = ""
        try:
            while True:
                raw_line = await stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if mode == "quick":
                    match = TRYCLOUDFLARE_URL_RE.search(line)
                    if match:
                        self._public_url = match.group(0)
                        self._url_ready_event.set()
                        log.info("Cloudflare Quick Tunnel đã sẵn sàng tại: %s", self._public_url)
                lowered = line.lower()
                if "err" in lowered or "failed" in lowered or "unauthorized" in lowered or "invalid" in lowered:
                    last_err_line = redact_secrets(line, max_len=240)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Lỗi đọc stderr từ cloudflared: %s", redact_secrets(str(exc)))
        finally:
            rc = getattr(proc, "returncode", None)
            if rc is not None and rc != 0 and self._process is proc:
                self._last_error = last_err_line or f"cloudflared thoát bất thường với mã {rc}"
                log.warning("Tiến trình cloudflared đã dừng (exit=%s): %s", rc, self._last_error)

    async def _stop_locked(self) -> None:
        """Dừng tiến trình `cloudflared` hiện tại và hủy tác vụ đọc stderr (phải gọi trong khóa)."""
        proc = self._process
        stderr_task = self._stderr_task
        self._process = None
        self._stderr_task = None
        self._public_url = ""
        self._started_at = None

        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                log.warning("Lỗi khi dừng tiến trình cloudflared: %s", redact_secrets(str(exc)))

        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    async def stop(self) -> dict[str, Any]:
        """Dừng an toàn tiến trình `cloudflared` (dùng khi tắt trên Dashboard hoặc trong `bot.close()`)."""
        async with self._lock:
            self._enabled = False
            await self._stop_locked()
            self._last_error = None
            log.info("Đã dừng Cloudflare Tunnel.")
            return self.status_snapshot()

    def set_tunnel_only_inbound(self, enabled: bool) -> None:
        """Cập nhật trạng thái telemetry `tunnel_only_inbound` ngay trong RAM mà không làm gián đoạn tiến trình `cloudflared`."""
        self._tunnel_only_inbound = bool(enabled)

    async def apply_config(self, tunnel_cfg: dict[str, Any]) -> dict[str, Any]:
        """Áp dụng cấu hình `tunnel` nóng theo thời gian thực: khởi động, khởi động lại hoặc dừng `cloudflared`."""
        enabled = bool(tunnel_cfg.get("cf_tunnel_enabled", False))
        mode = str(tunnel_cfg.get("cf_tunnel_mode") or "named").strip().lower()
        if mode not in ("named", "quick"):
            mode = "named"
        raw_token = str(tunnel_cfg.get("cf_tunnel_token") or "").strip()
        tunnel_only = bool(tunnel_cfg.get("tunnel_only_inbound", False))

        async with self._lock:
            self._enabled = enabled
            self._mode = mode
            self._tunnel_only_inbound = tunnel_only
            await self._stop_locked()
            self._last_error = None

            if not enabled:
                return self.status_snapshot()

            token = ""
            if mode == "named":
                if not raw_token:
                    self._last_error = "Thiếu Cloudflare Tunnel Token cho chế độ Named Tunnel."
                    log.warning("Không thể khởi động Named Tunnel: %s", self._last_error)
                    return self.status_snapshot()
                try:
                    token = validate_cloudflare_tunnel_token(raw_token)
                except ValueError as exc:
                    self._last_error = str(exc)
                    log.warning("Từ chối khởi động Named Tunnel do token không hợp lệ.")
                    return self.status_snapshot()

            cmd = self.build_command(mode, token)
            child_env = self.build_subprocess_env(mode, token)
            self._url_ready_event = asyncio.Event()
            try:
                proc = await self._spawn(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    env=child_env,
                )
            except FileNotFoundError:
                self._last_error = f"Không tìm thấy binary '{self.binary_path}' trong container."
                log.warning("Không khởi động được Cloudflare Tunnel: %s", self._last_error)
                return self.status_snapshot()
            except Exception as exc:
                self._last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=240)
                log.warning("Lỗi khởi chạy tiến trình cloudflared: %s", self._last_error)
                return self.status_snapshot()

            self._process = proc
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._stderr_task = asyncio.create_task(self._consume_stderr(proc, mode))
            log.info(
                "Đã khởi chạy Cloudflare Tunnel (mode=%s, pid=%s, origin=%s).",
                mode,
                getattr(proc, "pid", None),
                self.origin_url,
            )

            # Ở chế độ Quick Tunnel, chờ ngắn để bắt URL trycloudflare.com trả ngay cho Dashboard
            if mode == "quick":
                try:
                    await asyncio.wait_for(self._url_ready_event.wait(), timeout=self._quick_url_wait_seconds)
                except asyncio.TimeoutError:
                    pass

            return self.status_snapshot()

    def attach_warp_proxy(self, warp_proxy: Optional[Any]) -> None:
        """Gắn `WarpProxyService` vào `CloudflareTunnelService` để tổng hợp trạng thái Outbound WARP chung trên `/api/status`."""
        self._warp_proxy = warp_proxy

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất dữ liệu telemetry trực tiếp của Cloudflare Tunnel và Cloudflare WARP Outbound cho API `/api/status` và Dashboard."""
        running = self.is_running
        pid = getattr(self._process, "pid", None) if running else None
        warp_snap = self._warp_proxy.status_snapshot() if self._warp_proxy is not None else None
        if warp_snap is not None:
            outbound_mode = warp_snap["outbound_mode"]
            outbound_configured = bool(self.outbound_proxy_url or warp_snap["connected"] or warp_snap["active"])
        else:
            outbound_configured = bool(self.outbound_proxy_url)
            outbound_mode = "proxy" if outbound_configured else "direct"
        snapshot: dict[str, Any] = {
            "enabled": self._enabled,
            "running": running,
            "mode": self._mode,
            "public_url": self._public_url if running else "",
            "origin_url": self.origin_url,
            "pid": pid,
            "last_error": self._last_error,
            "tunnel_only_inbound": self._tunnel_only_inbound,
            "outbound_mode": outbound_mode,
            "outbound_proxy_configured": outbound_configured,
            "started_at": self._started_at if running else None,
        }
        if warp_snap is not None:
            snapshot.update({
                "warp": warp_snap,
                "warp_enabled": warp_snap["enabled"],
                "warp_connected": warp_snap["connected"],
                "warp_active": warp_snap["active"],
                "warp_fallback_direct": warp_snap["fallback_direct"],
                "warp_installed": warp_snap.get("warp_installed", False),
                "warp_ram_mb": warp_snap["warp_ram_mb"],
                "warp_pid": warp_snap["warp_pid"],
                "warp_proxy_url": warp_snap["proxy_url"],
                "warp_proxy_port": warp_snap.get("proxy_port", 40000),
                "warp_diagnostic": warp_snap.get("diagnostic"),
                "warp_diagnostic_code": warp_snap.get("diagnostic_code"),
            })
        return snapshot
