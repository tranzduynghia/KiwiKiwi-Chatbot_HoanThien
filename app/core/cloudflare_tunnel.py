"""Dịch vụ quản lý tiến trình con `cloudflared` bất đồng bộ (Named Tunnel Token & Quick Tunnel) không cần restart bot."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("gemini-bot.tunnel")

TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


class CloudflareTunnelService:
    """Điều phối vòng đời tiến trình con `cloudflared` bằng `asyncio.create_subprocess_exec` và xuất telemetry trực tiếp."""

    def __init__(
        self,
        *,
        local_port: int = 10000,
        outbound_proxy_url: str = "",
        binary_path: str = "cloudflared",
        spawn_subprocess: Optional[Callable[..., Awaitable[Any]]] = None,
        quick_url_wait_seconds: float = 2.0,
    ):
        self.local_port = int(local_port)
        self.outbound_proxy_url = (outbound_proxy_url or "").strip()
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
        """Xây dựng danh sách tham số CLI cho `cloudflared` theo chế độ Named Tunnel hoặc Quick Tunnel."""
        clean_mode = (mode or "named").strip().lower()
        if clean_mode == "quick":
            return [
                self.binary_path,
                "tunnel",
                "--no-autoupdate",
                "--url",
                self.origin_url,
            ]
        return [
            self.binary_path,
            "tunnel",
            "--no-autoupdate",
            "--edge-ip-version",
            "auto",
            "run",
            "--token",
            token.strip(),
        ]

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
                    last_err_line = line[:240]
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Lỗi đọc stderr từ cloudflared: %s", exc)
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
                log.warning("Lỗi khi dừng tiến trình cloudflared: %s", exc)

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

    async def apply_config(self, tunnel_cfg: dict[str, Any]) -> dict[str, Any]:
        """Áp dụng cấu hình `tunnel` nóng theo thời gian thực: khởi động, khởi động lại hoặc dừng `cloudflared`."""
        enabled = bool(tunnel_cfg.get("cf_tunnel_enabled", False))
        mode = str(tunnel_cfg.get("cf_tunnel_mode") or "named").strip().lower()
        if mode not in ("named", "quick"):
            mode = "named"
        token = str(tunnel_cfg.get("cf_tunnel_token") or "").strip()
        tunnel_only = bool(tunnel_cfg.get("tunnel_only_inbound", False))

        async with self._lock:
            self._enabled = enabled
            self._mode = mode
            self._tunnel_only_inbound = tunnel_only
            await self._stop_locked()
            self._last_error = None

            if not enabled:
                return self.status_snapshot()

            if mode == "named" and not token:
                self._last_error = "Thiếu Cloudflare Tunnel Token cho chế độ Named Tunnel."
                log.warning("Không thể khởi động Named Tunnel: %s", self._last_error)
                return self.status_snapshot()

            cmd = self.build_command(mode, token)
            self._url_ready_event = asyncio.Event()
            try:
                proc = await self._spawn(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                self._last_error = f"Không tìm thấy binary '{self.binary_path}' trong container."
                log.warning("Không khởi động được Cloudflare Tunnel: %s", self._last_error)
                return self.status_snapshot()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:240]
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

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất dữ liệu telemetry trực tiếp của Cloudflare Tunnel cho API `/api/status` và Dashboard."""
        running = self.is_running
        pid = getattr(self._process, "pid", None) if running else None
        outbound_configured = bool(self.outbound_proxy_url)
        return {
            "enabled": self._enabled,
            "running": running,
            "mode": self._mode,
            "public_url": self._public_url if running else "",
            "origin_url": self.origin_url,
            "pid": pid,
            "last_error": self._last_error,
            "tunnel_only_inbound": self._tunnel_only_inbound,
            "outbound_mode": "proxy" if outbound_configured else "direct",
            "outbound_proxy_configured": outbound_configured,
            "started_at": self._started_at if running else None,
        }
