"""Dịch vụ giám sát và định tuyến Outbound qua Cloudflare WARP User-Space Proxy (127.0.0.1:40000) kèm cơ chế tự động fallback Direct."""
from __future__ import annotations

import asyncio
import inspect
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse, urlunparse

import psutil

from app.core.security import redact_secrets

log = logging.getLogger("gemini-bot.warp")

DEFAULT_WARP_PROXY_HOST = "127.0.0.1"
DEFAULT_WARP_PROXY_PORT = 40000
DEFAULT_WARP_PROXY_URL = f"http://{DEFAULT_WARP_PROXY_HOST}:{DEFAULT_WARP_PROXY_PORT}"
WARP_PROCESS_NAMES = frozenset({"warp-svc", "warp-svc.exe"})


def parse_proxy_host_port(proxy_url: str) -> tuple[str, int]:
    """Tách cặp `(host, port)` từ URL proxy (mặc định `127.0.0.1:40000` nếu thiếu)."""
    raw = (proxy_url or "").strip()
    if not raw:
        return DEFAULT_WARP_PROXY_HOST, DEFAULT_WARP_PROXY_PORT
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname or DEFAULT_WARP_PROXY_HOST
        port = parsed.port or DEFAULT_WARP_PROXY_PORT
        return host, int(port)
    except ValueError:
        return DEFAULT_WARP_PROXY_HOST, DEFAULT_WARP_PROXY_PORT


def sanitize_proxy_url_display(proxy_url: str) -> str:
    """Loại bỏ hoàn toàn thông tin xác thực (`user:pass@`) nếu có trước khi hiển thị proxy URL ra telemetry/log."""
    raw = (proxy_url or "").strip()
    if not raw:
        return DEFAULT_WARP_PROXY_URL
    try:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.hostname:
            return DEFAULT_WARP_PROXY_URL
        netloc = parsed.hostname
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        return urlunparse((parsed.scheme.lower(), netloc, "", "", "", ""))
    except ValueError:
        return DEFAULT_WARP_PROXY_URL


def probe_tcp_proxy_sync(host: str, port: int, timeout: float = 0.2) -> bool:
    """Kiểm tra đồng bộ nhanh xem cổng WARP proxy cục bộ (vd: 127.0.0.1:40000) có đang mở kết nối hay không."""
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.05, float(timeout))):
            return True
    except OSError:
        return False


async def probe_tcp_proxy_async(host: str, port: int, timeout: float = 0.25) -> bool:
    """Kiểm tra bất đồng bộ nhanh xem cổng WARP proxy cục bộ có đang nhận kết nối TCP hay không."""
    writer = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)),
            timeout=max(0.05, float(timeout)),
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def read_warp_process_metrics() -> tuple[Optional[int], Optional[float]]:
    """Đọc PID và dung lượng RAM (RSS, tính bằng MB) của tiến trình `warp-svc` nếu đang chạy trong container."""
    total_rss = 0
    primary_pid: Optional[int] = None
    try:
        for proc in psutil.process_iter(["pid", "name", "memory_info"]):
            try:
                info = proc.info
                name = str(info.get("name") or "").strip().lower()
                if name not in WARP_PROCESS_NAMES:
                    continue
                if primary_pid is None:
                    primary_pid = int(info["pid"])
                mem_info = info.get("memory_info")
                if mem_info is not None:
                    total_rss += int(getattr(mem_info, "rss", 0) or 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        return None, None
    if primary_pid is None:
        return None, None
    return primary_pid, round(total_rss / (1024 * 1024), 1)


def is_proxy_transport_error(exc: BaseException) -> bool:
    """Nhận diện lỗi kết nối proxy cục bộ (ProxyError / ConnectError / ConnectionRefused) để kích hoạt fallback Direct."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        cls_name = type(cur).__name__.lower()
        if any(tok in cls_name for tok in ("proxyerror", "connecterror", "connecttimeout", "clientproxyconnectionerror")):
            return True
        if isinstance(cur, (ConnectionRefusedError, BrokenPipeError)):
            return True
        msg = str(cur).lower()
        if "proxy" in msg and any(tok in msg for tok in ("refused", "unreachable", "failed", "reset", "error")):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class WarpProxyService:
    """Quản lý trạng thái Cloudflare WARP user-space proxy, tự động fallback Direct và đồng bộ nóng sang Gemini + Discord."""

    def __init__(
        self,
        *,
        proxy_url: str = "",
        warp_port: int = DEFAULT_WARP_PROXY_PORT,
        enabled: bool = True,
        explicit_proxy_url: bool = False,
        probe_sync_fn: Optional[Callable[[str, int, float], bool]] = None,
        probe_async_fn: Optional[Callable[[str, int, float], Awaitable[bool]]] = None,
        metrics_fn: Optional[Callable[[], tuple[Optional[int], Optional[float]]]] = None,
        cache_ttl: float = 2.0,
        probe_timeout: float = 0.25,
    ):
        clean_port = int(warp_port)
        if not (1 <= clean_port <= 65535):
            clean_port = DEFAULT_WARP_PROXY_PORT

        raw_url = (proxy_url or "").strip()
        self.explicit_proxy_url = bool(explicit_proxy_url and raw_url)
        self.proxy_url = raw_url or f"http://{DEFAULT_WARP_PROXY_HOST}:{clean_port}"
        self.safe_proxy_url = sanitize_proxy_url_display(self.proxy_url)
        self.host, self.port = parse_proxy_host_port(self.proxy_url)

        self._probe_sync = probe_sync_fn or probe_tcp_proxy_sync
        self._probe_async = probe_async_fn or probe_tcp_proxy_async
        self._metrics_fn = metrics_fn or read_warp_process_metrics
        self._cache_ttl = max(0.2, float(cache_ttl))
        self._probe_timeout = max(0.05, float(probe_timeout))

        self._lock = asyncio.Lock()
        self._route_hooks: list[Callable[[Optional[str]], Any]] = []

        self._enabled: bool = bool(enabled)
        self._connected: bool = False
        self._probed_once: bool = False
        self._applied_proxy_url: Optional[str] = None
        self._ram_mb: Optional[float] = None
        self._pid: Optional[int] = None
        self._last_checked_mono: float = 0.0
        self._last_checked_iso: Optional[str] = None
        self._last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        """Cho biết công tắc WARP Outbound trên Dashboard/cấu hình có đang bật hay không."""
        return self._enabled

    @property
    def connected(self) -> bool:
        """Cho biết cổng WARP proxy (127.0.0.1:40000) có đang nhận kết nối TCP ở lần kiểm tra gần nhất hay không."""
        return self._connected

    @property
    def is_active(self) -> bool:
        """Cho biết lưu lượng Outbound (Gemini & Discord) hiện có đang thực sự đi qua WARP proxy hay không."""
        if not self._enabled:
            return False
        if self._probed_once:
            return self._connected
        # Trước khi chạy kiểm tra mạng đầu tiên, tôn trọng OUTBOUND_PROXY_URL cấu hình tường minh
        return self.explicit_proxy_url

    def effective_proxy_url(self) -> str:
        """Trả về URL proxy hiệu lực (`http://127.0.0.1:40000`) nếu WARP đang bật & sẵn sàng, ngược lại trả về `""` (Direct)."""
        return self.proxy_url if self.is_active else ""

    def register_route_hook(self, hook: Callable[[Optional[str]], Any]) -> None:
        """Đăng ký callback nhận `proxy_url | None` mỗi khi tuyến Outbound chuyển giữa WARP và Direct."""
        self._route_hooks.append(hook)

    def probe_initial_sync(self) -> bool:
        """Kiểm tra đồng bộ nhanh lúc khởi động container (bỏ qua nếu `OUTBOUND_PROXY_URL` được chỉ định tường minh)."""
        if self.explicit_proxy_url:
            self._applied_proxy_url = self.proxy_url if self._enabled else None
            return True
        if not self._enabled:
            self._connected = False
            self._probed_once = True
            self._applied_proxy_url = None
            return False
        reachable = bool(self._probe_sync(self.host, self.port, min(0.2, self._probe_timeout)))
        self._connected = reachable
        self._probed_once = True
        self._last_checked_mono = time.monotonic()
        self._last_checked_iso = datetime.now(timezone.utc).isoformat()
        self._pid, self._ram_mb = self._metrics_fn()
        self._applied_proxy_url = self.proxy_url if reachable else None
        if reachable:
            log.info("Đã phát hiện Cloudflare WARP proxy sẵn sàng tại %s (RAM: %s MB).", self.safe_proxy_url, self._ram_mb)
        else:
            log.info("Chưa phát hiện WARP proxy tại %s — tự động dùng kết nối Outbound Direct.", self.safe_proxy_url)
        return reachable

    async def _apply_route_hooks(self, target_proxy: Optional[str]) -> None:
        """Gọi toàn bộ callback cập nhật tuyến Outbound cho Gemini Client và Discord HTTPClient."""
        previous = self._applied_proxy_url
        self._applied_proxy_url = target_proxy
        if previous == target_proxy:
            return
        for hook in list(self._route_hooks):
            try:
                result = hook(target_proxy)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log.warning("Lỗi cập nhật route hook WARP Outbound: %s", redact_secrets(str(exc)))
        if target_proxy:
            log.info("Đã chuyển định tuyến Outbound (Gemini & Discord) sang Cloudflare WARP (%s).", self.safe_proxy_url)
        else:
            log.info("Đã chuyển định tuyến Outbound (Gemini & Discord) sang kết nối trực tiếp (Direct).")

    async def refresh_status(self, *, force: bool = False) -> dict[str, Any]:
        """Kiểm tra tình trạng kết nối WARP proxy và RAM của `warp-svc` (có cache ngắn); tự động fallback/khôi phục khi cần."""
        now = time.monotonic()
        if not force and self._probed_once and (now - self._last_checked_mono) < self._cache_ttl:
            return self.status_snapshot()

        async with self._lock:
            now = time.monotonic()
            if not force and self._probed_once and (now - self._last_checked_mono) < self._cache_ttl:
                return self.status_snapshot()

            reachable = False
            try:
                reachable = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
                self._last_error = None
            except Exception as exc:
                self._last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=200)
                reachable = False

            self._connected = reachable
            self._probed_once = True
            self._last_checked_mono = time.monotonic()
            self._last_checked_iso = datetime.now(timezone.utc).isoformat()
            self._pid, self._ram_mb = self._metrics_fn()

            desired_proxy = self.proxy_url if (self._enabled and self._connected) else None
            if desired_proxy != self._applied_proxy_url:
                if self._enabled and not self._connected and self._applied_proxy_url is not None:
                    log.warning(
                        "WARP proxy %s không phản hồi — tự động fallback sang Outbound Direct.",
                        self.safe_proxy_url,
                    )
                await self._apply_route_hooks(desired_proxy)

            return self.status_snapshot()

    async def set_enabled(self, enabled: bool, *, force_probe: bool = True) -> dict[str, Any]:
        """Bật hoặc tắt định tuyến Outbound qua Cloudflare WARP ngay lập tức không cần restart bot."""
        async with self._lock:
            self._enabled = bool(enabled)
            if force_probe:
                try:
                    self._connected = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
                    self._last_error = None
                except Exception as exc:
                    self._last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=200)
                    self._connected = False
                self._probed_once = True
                self._last_checked_mono = time.monotonic()
                self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                self._pid, self._ram_mb = self._metrics_fn()

            desired_proxy = self.proxy_url if (self._enabled and self._connected) else None
            await self._apply_route_hooks(desired_proxy)
            return self.status_snapshot()

    async def fallback_to_direct(self, reason: str = "") -> bool:
        """Tự động hạ tuyến về Direct ngay giữa request nếu WARP proxy đột ngột ngắt kết nối."""
        async with self._lock:
            was_using_proxy = self._applied_proxy_url is not None
            self._connected = False
            self._probed_once = True
            self._last_checked_mono = time.monotonic()
            self._last_checked_iso = datetime.now(timezone.utc).isoformat()
            if reason:
                self._last_error = redact_secrets(reason, max_len=200)
            if was_using_proxy:
                log.warning(
                    "Phát hiện lỗi kết nối WARP proxy (%s) — lập tức chuyển sang Direct.",
                    self._last_error or "unreachable",
                )
                await self._apply_route_hooks(None)
            return was_using_proxy

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất telemetry gọn nhẹ về Cloudflare WARP cho API `/api/status` và Dashboard (không chứa bí mật)."""
        active = self.is_active
        fallback_direct = bool(self._enabled and self._probed_once and not self._connected)
        return {
            "enabled": self._enabled,
            "connected": self._connected,
            "active": active,
            "fallback_direct": fallback_direct,
            "proxy_url": self.safe_proxy_url,
            "outbound_mode": "proxy" if active else "direct",
            "warp_ram_mb": self._ram_mb,
            "warp_pid": self._pid,
            "last_checked_at": self._last_checked_iso,
            "last_error": self._last_error,
        }
