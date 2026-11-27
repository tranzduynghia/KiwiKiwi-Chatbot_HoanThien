"""Dịch vụ quản lý Cloudflare WARP User-Space Proxy bằng binary tĩnh `wgcf` + `wireproxy` (SOCKS5 `127.0.0.1:40000`, ~10 MB RAM) kèm tự động fallback Direct."""
from __future__ import annotations

import asyncio
import configparser
import hashlib
import inspect
import logging
import os
import re
import shutil
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
DEFAULT_WARP_PROXY_URL = f"socks5h://{DEFAULT_WARP_PROXY_HOST}:{DEFAULT_WARP_PROXY_PORT}"
DEFAULT_WARP_STATE_DIR = "/var/lib/warp-proxy"
WARP_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
WARP_PROCESS_NAMES = frozenset({"wireproxy", "wireproxy.exe"})
WARP_REQUIRED_BINARIES = ("wgcf", "wireproxy")
NOT_INSTALLED_MESSAGE = "Chưa cài wgcf/wireproxy"
_WARP_FALLBACK_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin")

ACCOUNT_FILE = "wgcf-account.toml"
PROFILE_FILE = "wgcf-profile.conf"
WIREPROXY_CONFIG_FILE = "wireproxy.conf"
WIREPROXY_LOG_FILE = "wireproxy.log"
LICENSE_MARKER_FILE = ".license-applied"

# Các biến môi trường an toàn được phép truyền cho tiến trình con `wgcf`/`wireproxy` (tuyệt đối không lộ bí mật bot)
_ALLOWED_WARP_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# Các trường được phép sao chép từ `wgcf-profile.conf` sang `wireproxy.conf`
_INTERFACE_KEYS = ("PrivateKey", "Address", "DNS", "MTU")
_PEER_KEYS = ("PublicKey", "AllowedIPs", "Endpoint", "PersistentKeepalive")

# Gỡ đường dẫn hệ thống nội bộ, khóa WireGuard và IP máy chủ thô khỏi thông điệp chẩn đoán trả về giao diện
_INTERNAL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._+-]+){2,}")
_WG_KEY_LINE_RE = re.compile(r"(?i)\b(private_?key|license_?key|access_?token|device_?id)\b\s*[=:]\s*\S+")
_WG_B64_KEY_RE = re.compile(r"\b[A-Za-z0-9+/]{42,43}=")
_IPV4_RE = re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"(?<![\w:])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){3,7}(?![\w:])")


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


def _license_key_from_env() -> str:
    """Đọc WARP+ license key tùy chọn từ `WARP_LICENSE_KEY` (hoặc `WGCF_LICENSE_KEY`) — chỉ dùng cho tiến trình `wgcf update`."""
    return ((os.getenv("WARP_LICENSE_KEY") or os.getenv("WGCF_LICENSE_KEY") or "")).strip()


def sanitize_warp_diagnostic(raw_text: str, *, max_len: int = 180) -> str:
    """Làm sạch stderr/log của `wgcf`/`wireproxy`: che bí mật, khóa WireGuard, IP máy chủ thô và đường dẫn nội bộ."""
    cleaned = redact_secrets(raw_text or "", max_len=max_len * 3)
    for env_name in ("WARP_LICENSE_KEY", "WGCF_LICENSE_KEY", "WARP_CONNECTOR_TOKEN"):
        secret_val = (os.getenv(env_name) or "").strip()
        if secret_val and len(secret_val) >= 4:
            cleaned = cleaned.replace(secret_val, "[REDACTED]")
    cleaned = _WG_KEY_LINE_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", cleaned)
    cleaned = _WG_B64_KEY_RE.sub("[REDACTED]", cleaned)
    cleaned = _IPV4_RE.sub("[ip]", cleaned)
    cleaned = _IPV6_RE.sub("[ip]", cleaned)
    cleaned = _INTERNAL_PATH_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :;-")
    return cleaned[:max_len]


def build_warp_subprocess_env(*, license_key: str = "") -> dict[str, str]:
    """Tạo môi trường tối giản cho `wgcf`/`wireproxy`; chỉ đưa `WGCF_LICENSE_KEY` vào khi gọi `wgcf update`."""
    env = {
        key: os.environ[key]
        for key in _ALLOWED_WARP_ENV_KEYS
        if key in os.environ and os.environ[key]
    }
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    if license_key:
        env["WGCF_LICENSE_KEY"] = license_key
    return env


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
    """Đọc PID và dung lượng RAM (RSS, MB) của tiến trình `wireproxy` nếu đang chạy trong container."""
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


def terminate_external_wireproxy(exclude_pids: Optional[set[int]] = None) -> int:
    """Dừng các tiến trình `wireproxy` không do Python quản lý (vd: do entrypoint khởi chạy) để áp dụng lại cấu hình."""
    exclude = exclude_pids or set()
    stopped = 0
    try:
        procs = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if str(proc.info.get("name") or "").lower() in WARP_PROCESS_NAMES and proc.pid not in exclude:
                    proc.terminate()
                    procs.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        _gone, alive = psutil.wait_procs(procs, timeout=2.0)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        stopped = len(procs)
    except Exception:
        return stopped
    return stopped


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


def parse_wgcf_profile(profile_text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Phân tích `wgcf-profile.conf` thành hai từ điển `[Interface]` và `[Peer]` (giữ nguyên chữ hoa/thường của khóa)."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment]
    parser.read_string(profile_text or "")
    if not parser.has_section("Interface") or not parser.has_section("Peer"):
        raise ValueError("wgcf-profile.conf thiếu mục [Interface] hoặc [Peer]")
    interface = {k: parser.get("Interface", k).strip() for k in _INTERFACE_KEYS if parser.has_option("Interface", k)}
    peer = {k: parser.get("Peer", k).strip() for k in _PEER_KEYS if parser.has_option("Peer", k)}
    for required, section in (("PrivateKey", interface), ("Address", interface), ("PublicKey", peer), ("Endpoint", peer)):
        if not section.get(required):
            raise ValueError(f"wgcf-profile.conf thiếu trường {required}")
    return interface, peer


def render_wireproxy_config(
    interface: dict[str, str],
    peer: dict[str, str],
    *,
    socks_port: int,
    http_port: Optional[int] = None,
) -> str:
    """Sinh nội dung `wireproxy.conf` với `[Socks5]` (và `[http]` tùy chọn) khóa chặt vào loopback `127.0.0.1`."""
    lines = ["[Interface]"]
    lines += [f"{k} = {interface[k]}" for k in _INTERFACE_KEYS if interface.get(k)]
    lines += ["", "[Peer]"]
    lines += [f"{k} = {peer[k]}" for k in _PEER_KEYS if peer.get(k)]
    lines += ["", "[Socks5]", f"BindAddress = {DEFAULT_WARP_PROXY_HOST}:{int(socks_port)}"]
    if http_port:
        lines += ["", "[http]", f"BindAddress = {DEFAULT_WARP_PROXY_HOST}:{int(http_port)}"]
    return "\n".join(lines) + "\n"


def parse_trace_payload(text: str) -> dict[str, Any]:
    """Rút gọn phản hồi `cdn-cgi/trace` thành `warp`/`colo`/`loc`/`http` — tuyệt đối không giữ IP thô (`ip=`)."""
    fields: dict[str, str] = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip().lower()] = value.strip()
    warp_state = fields.get("warp", "").lower() or "unknown"
    return {
        "warp": warp_state,
        "warp_ok": warp_state in ("on", "plus"),
        "colo": re.sub(r"[^A-Za-z0-9]", "", fields.get("colo", ""))[:8] or None,
        "loc": re.sub(r"[^A-Za-z]", "", fields.get("loc", ""))[:4] or None,
        "http": re.sub(r"[^A-Za-z0-9/.]", "", fields.get("http", ""))[:12] or None,
    }


async def fetch_trace_via_proxy(proxy_url: str, timeout: float = 6.0) -> str:
    """Gọi `https://www.cloudflare.com/cdn-cgi/trace` qua `socks5h://127.0.0.1:<port>` (DNS phân giải phía WARP, chống rò rỉ)."""
    import httpx

    async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, trust_env=False) as client:
        resp = await client.get(WARP_TRACE_URL)
        resp.raise_for_status()
        return resp.text


def _read_sanitized_log_tail(log_path: str, *, max_bytes: int = 4096) -> str:
    """Đọc dòng lỗi gần nhất trong log `wireproxy` đã được làm sạch bí mật, khóa, IP và đường dẫn."""
    try:
        if not log_path or not os.path.isfile(log_path):
            return ""
        with open(log_path, "rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = fp.read().decode("utf-8", errors="replace")
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        if not lines:
            return ""
        for line in reversed(lines[-15:]):
            lowered = line.lower()
            if lowered.startswith("debug:"):
                continue
            if any(tok in lowered for tok in ("error", "fail", "denied", "cannot", "unable", "panic", "refused", "in use")):
                return sanitize_warp_diagnostic(line)
        return ""
    except OSError:
        return ""


class WarpProxyService:
    """Quản lý vòng đời `wgcf` + `wireproxy`, kiểm tra socket `127.0.0.1:<port>`, xác minh `cdn-cgi/trace`, tự fallback Direct và đồng bộ nóng sang Gemini + Discord."""

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
        which_fn: Optional[Callable[[str], Optional[str]]] = None,
        spawn_subprocess: Optional[Callable[..., Awaitable[Any]]] = None,
        trace_fn: Optional[Callable[[str], Awaitable[str]]] = None,
        terminate_external_fn: Optional[Callable[[set[int]], int]] = None,
        state_dir: Optional[str] = None,
        http_port: Optional[int] = None,
        cache_ttl: float = 2.0,
        probe_timeout: float = 0.25,
        connect_poll_timeout: float = 7.0,
        connect_poll_interval: float = 0.5,
        cli_timeout: float = 25.0,
        verify_trace_on_start: bool = True,
        log_path: Optional[str] = None,
        **_legacy_kwargs: Any,
    ):
        clean_port = int(warp_port)
        if not (1 <= clean_port <= 65535):
            clean_port = DEFAULT_WARP_PROXY_PORT

        raw_url = (proxy_url or "").strip()
        self.explicit_proxy_url = bool(explicit_proxy_url and raw_url)
        self.proxy_url = raw_url or f"socks5h://{DEFAULT_WARP_PROXY_HOST}:{clean_port}"
        self.safe_proxy_url = sanitize_proxy_url_display(self.proxy_url)
        self.host, self.port = parse_proxy_host_port(self.proxy_url)

        self._probe_sync = probe_sync_fn or probe_tcp_proxy_sync
        self._probe_async = probe_async_fn or probe_tcp_proxy_async
        self._metrics_fn = metrics_fn or read_warp_process_metrics
        self._custom_which = which_fn is not None
        self._which = which_fn or shutil.which
        self._spawn = spawn_subprocess or asyncio.create_subprocess_exec
        self._trace_fn = trace_fn or fetch_trace_via_proxy
        self._terminate_external = terminate_external_fn or terminate_external_wireproxy
        self.state_dir = state_dir or (os.getenv("WARP_STATE_DIR") or "").strip() or DEFAULT_WARP_STATE_DIR
        env_http_port = (os.getenv("WARP_HTTP_PROXY_PORT") or "").strip()
        if http_port is None and env_http_port.isdigit():
            http_port = int(env_http_port)
        self.http_port = http_port if (http_port and 1 <= int(http_port) <= 65535 and int(http_port) != self.port) else None
        self._cache_ttl = max(0.01, float(cache_ttl))
        self._probe_timeout = max(0.05, float(probe_timeout))
        self._connect_poll_timeout = max(0.2, float(connect_poll_timeout))
        self._connect_poll_interval = max(0.02, float(connect_poll_interval))
        self._cli_timeout = max(0.5, float(cli_timeout))
        self._verify_trace_on_start = bool(verify_trace_on_start)
        self._log_path = log_path or ""

        self._lock = asyncio.Lock()
        self._route_hooks: list[Callable[[Optional[str]], Any]] = []
        self._daemon_proc: Any = None
        self._daemon_log_fp: Any = None

        self._enabled: bool = bool(enabled)
        self._connected: bool = False
        self._probed_once: bool = False
        self._applied_proxy_url: Optional[str] = None
        self._ram_mb: Optional[float] = None
        self._pid: Optional[int] = None
        self._last_checked_mono: float = 0.0
        self._last_checked_iso: Optional[str] = None
        self._last_error: Optional[str] = None
        self._diagnostic: Optional[str] = None
        self._diagnostic_code: str = "idle"
        self._trace: Optional[dict[str, Any]] = None

    @property
    def enabled(self) -> bool:
        """Cho biết công tắc WARP Outbound trên Dashboard/cấu hình có đang bật hay không."""
        return self._enabled

    @property
    def connected(self) -> bool:
        """Cho biết cổng WARP proxy (127.0.0.1:<port>) có đang nhận kết nối TCP ở lần kiểm tra gần nhất hay không."""
        return self._connected

    @property
    def is_active(self) -> bool:
        """Cho biết lưu lượng Outbound (Gemini & Discord) hiện có đang thực sự đi qua WARP proxy hay không."""
        if not self._enabled:
            return False
        if self._probed_once:
            return self._connected
        return self.explicit_proxy_url

    def _path(self, filename: str) -> str:
        """Ghép đường dẫn tệp trạng thái bên trong thư mục `/var/lib/warp-proxy`."""
        return os.path.join(self.state_dir, filename)

    def _find_binary(self, name: str) -> Optional[str]:
        """Tìm đường dẫn thực thi của `wgcf`/`wireproxy` trong `PATH` và các thư mục hệ thống chuẩn."""
        found = self._which(name)
        if found:
            return found
        if self._custom_which:
            return None
        for directory in _WARP_FALLBACK_BIN_DIRS:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def missing_binaries(self) -> list[str]:
        """Liệt kê các binary bắt buộc (`wgcf`, `wireproxy`) còn thiếu trong container."""
        return [name for name in WARP_REQUIRED_BINARIES if not self._find_binary(name)]

    def is_installed(self) -> bool:
        """Kiểm tra cả hai binary tĩnh `wgcf` và `wireproxy` có tồn tại trong container hay không."""
        return not self.missing_binaries()

    def _not_installed_message(self) -> str:
        """Thông điệp chẩn đoán nêu rõ binary nào còn thiếu."""
        missing = self.missing_binaries()
        if missing and len(missing) < len(WARP_REQUIRED_BINARIES):
            return f"Chưa cài {missing[0]}"
        return NOT_INSTALLED_MESSAGE

    def effective_proxy_url(self) -> str:
        """Trả về URL proxy hiệu lực (`socks5h://127.0.0.1:40000`) nếu WARP đang bật & sẵn sàng, ngược lại trả về `""` (Direct)."""
        return self.proxy_url if self.is_active else ""

    def register_route_hook(self, hook: Callable[[Optional[str]], Any]) -> None:
        """Đăng ký callback nhận `proxy_url | None` mỗi khi tuyến Outbound chuyển giữa WARP và Direct."""
        self._route_hooks.append(hook)

    def _set_loopback_port(self, port: int) -> None:
        """Cập nhật cổng proxy cục bộ, luôn khóa chặt về loopback `socks5h://127.0.0.1:<port>` (chống rò rỉ DNS)."""
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            raise ValueError("Cổng WARP proxy phải nằm trong khoảng 1..65535.")
        self.host = DEFAULT_WARP_PROXY_HOST
        self.port = port_int
        self.proxy_url = f"socks5h://{DEFAULT_WARP_PROXY_HOST}:{port_int}"
        self.safe_proxy_url = self.proxy_url
        self.explicit_proxy_url = False
        if self.http_port == port_int:
            self.http_port = None

    def _set_diagnostic(self, code: str, message: Optional[str]) -> None:
        """Cập nhật mã và thông điệp chẩn đoán đã làm sạch cho thẻ WARP trên Dashboard."""
        self._diagnostic_code = code
        self._diagnostic = sanitize_warp_diagnostic(message) if message else None
        self._last_error = self._diagnostic

    def _mark_checked(self) -> None:
        """Ghi nhận mốc thời gian kiểm tra và đọc lại RAM/PID của `wireproxy`."""
        self._probed_once = True
        self._last_checked_mono = time.monotonic()
        self._last_checked_iso = datetime.now(timezone.utc).isoformat()
        self._pid, self._ram_mb = self._metrics_fn()

    def _managed_alive(self) -> bool:
        """Cho biết tiến trình `wireproxy` do Python khởi chạy còn sống hay không."""
        return self._daemon_proc is not None and getattr(self._daemon_proc, "returncode", None) is None

    def _infer_offline_diagnostic(self) -> tuple[str, str]:
        """Suy luận chẩn đoán rõ ràng khi WARP đang bật nhưng cổng loopback chưa phản hồi."""
        if not self.is_installed():
            return "not_installed", self._not_installed_message()
        pid, _ram = self._metrics_fn()
        if not self._managed_alive() and pid is None:
            if not os.path.isfile(self._path(WIREPROXY_CONFIG_FILE)):
                return "profile_missing", "Chưa tạo hồ sơ WARP (wgcf register/generate)"
            tail = _read_sanitized_log_tail(self._log_path or self._path(WIREPROXY_LOG_FILE))
            if tail:
                return "daemon_not_running", f"wireproxy chưa chạy ({tail})"
            return "daemon_not_running", "wireproxy chưa chạy"
        return "proxy_not_listening", f"127.0.0.1:{self.port} chưa phản hồi — đang dùng Direct fallback"

    def probe_initial_sync(self) -> bool:
        """Kiểm tra đồng bộ nhanh lúc khởi động container (bỏ qua nếu `OUTBOUND_PROXY_URL` được chỉ định tường minh)."""
        if self.explicit_proxy_url:
            self._applied_proxy_url = self.proxy_url if self._enabled else None
            return True
        if not self._enabled:
            self._connected = False
            self._probed_once = True
            self._applied_proxy_url = None
            self._set_diagnostic("disabled", None)
            return False
        reachable = bool(self._probe_sync(self.host, self.port, min(0.2, self._probe_timeout)))
        self._connected = reachable
        self._mark_checked()
        self._applied_proxy_url = self.proxy_url if reachable else None
        if reachable:
            self._set_diagnostic("connected", None)
            log.info("Đã phát hiện WARP proxy (wireproxy) sẵn sàng tại %s (RAM: %s MB).", self.safe_proxy_url, self._ram_mb)
        else:
            code, msg = self._infer_offline_diagnostic()
            self._set_diagnostic(code, msg)
            log.info("Chưa phát hiện WARP proxy tại %s (%s) — tự động dùng kết nối Outbound Direct.", self.safe_proxy_url, msg)
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

    async def _run_wgcf(self, *args: str, license_key: str = "") -> tuple[int, str]:
        """Chạy `wgcf <args>` an toàn trong `/var/lib/warp-proxy` (`shell=False`, env tối giản, giới hạn timeout); trả về `(rc, chẩn đoán đã che)`."""
        wgcf_bin = self._find_binary("wgcf")
        if not wgcf_bin:
            return 127, "Chưa cài wgcf"
        try:
            proc = await self._spawn(
                wgcf_bin,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.state_dir,
                env=build_warp_subprocess_env(license_key=license_key),
            )
        except FileNotFoundError:
            return 127, "Chưa cài wgcf"
        except Exception as exc:
            return 1, sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._cli_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return 124, f"wgcf {args[0] if args else ''} quá hạn thời gian"
        except Exception as exc:
            return 1, sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")

        rc = getattr(proc, "returncode", 0)
        rc = int(rc if rc is not None else 0)
        if rc == 0:
            return 0, ""
        text = (stderr_b or b"").decode("utf-8", errors="replace") or (stdout_b or b"").decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        err_lines = [ln for ln in lines if any(t in ln.lower() for t in ("error", "fail", "denied", "refused", "timeout", "unable"))]
        return rc, sanitize_warp_diagnostic((err_lines or lines or [""])[-1])

    def _write_private_file(self, path: str, content: str) -> None:
        """Ghi tệp chứa khóa WireGuard với quyền `0600` (chỉ chủ sở hữu đọc được)."""
        tmp_path = f"{path}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(content)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    async def _ensure_profile_locked(self) -> tuple[bool, Optional[str], str]:
        """Đảm bảo `wgcf-account.toml`, `wgcf-profile.conf` và `wireproxy.conf` (bind `127.0.0.1:<port>`) tồn tại và hợp lệ."""
        try:
            os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        except OSError as exc:
            return False, f"Không tạo được thư mục trạng thái WARP ({sanitize_warp_diagnostic(str(exc))})", "state_dir_error"
        if not os.access(self.state_dir, os.W_OK):
            return False, "Thư mục trạng thái WARP không ghi được", "state_dir_error"

        account_path = self._path(ACCOUNT_FILE)
        profile_path = self._path(PROFILE_FILE)
        marker_path = self._path(LICENSE_MARKER_FILE)
        regenerate = False

        if not os.path.isfile(account_path):
            rc, detail = await self._run_wgcf("register", "--accept-tos")
            if rc != 0 or not os.path.isfile(account_path):
                msg = f"Đăng ký tài khoản WARP thất bại ({detail})" if detail else "Đăng ký tài khoản WARP thất bại"
                return False, msg, "registration_failed"
            regenerate = True
            try:
                os.chmod(account_path, 0o600)
            except OSError:
                pass

        license_key = _license_key_from_env()
        if license_key:
            digest = hashlib.sha256(license_key.encode("utf-8")).hexdigest()
            applied = ""
            try:
                with open(marker_path, "r", encoding="utf-8") as fp:
                    applied = fp.read().strip()
            except OSError:
                applied = ""
            if applied != digest:
                rc, detail = await self._run_wgcf("update", license_key=license_key)
                if rc == 0:
                    self._write_private_file(marker_path, digest)
                    regenerate = True
                else:
                    log.warning("Không áp dụng được WARP_LICENSE_KEY: %s", detail or "rejected")

        if regenerate or not os.path.isfile(profile_path):
            rc, detail = await self._run_wgcf("generate")
            if rc != 0 or not os.path.isfile(profile_path):
                msg = f"Tạo hồ sơ WireGuard thất bại ({detail})" if detail else "Tạo hồ sơ WireGuard thất bại"
                return False, msg, "generate_failed"
            try:
                os.chmod(profile_path, 0o600)
            except OSError:
                pass

        try:
            with open(profile_path, "r", encoding="utf-8") as fp:
                interface, peer = parse_wgcf_profile(fp.read())
            content = render_wireproxy_config(interface, peer, socks_port=self.port, http_port=self.http_port)
            self._write_private_file(self._path(WIREPROXY_CONFIG_FILE), content)
        except (OSError, ValueError, configparser.Error) as exc:
            return False, f"Hồ sơ WireGuard không hợp lệ ({sanitize_warp_diagnostic(str(exc))})", "profile_invalid"
        return True, None, "profile_ready"

    def _close_daemon_log_handle(self) -> None:
        """Đóng file handle log của `wireproxy` an toàn nếu đang mở."""
        fp = self._daemon_log_fp
        self._daemon_log_fp = None
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass

    async def _stop_wireproxy_locked(self, *, include_external: bool) -> None:
        """Dừng `wireproxy` do Python quản lý (và tùy chọn cả tiến trình do entrypoint khởi chạy)."""
        proc = self._daemon_proc
        self._daemon_proc = None
        keep: set[int] = set()
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except Exception:
                pass
        self._close_daemon_log_handle()
        if include_external:
            try:
                await asyncio.to_thread(self._terminate_external, keep)
            except Exception:
                pass

    async def _spawn_wireproxy_locked(self) -> tuple[Any, str]:
        """Khởi chạy nền `wireproxy -c /var/lib/warp-proxy/wireproxy.conf` bằng `asyncio.create_subprocess_exec` (không shell)."""
        wireproxy_bin = self._find_binary("wireproxy")
        if not wireproxy_bin:
            raise FileNotFoundError("wireproxy")
        log_path = self._log_path or self._path(WIREPROXY_LOG_FILE)
        self._log_path = log_path
        self._close_daemon_log_handle()
        stdout_target: Any = asyncio.subprocess.DEVNULL
        try:
            fp = open(log_path, "wb")
            self._daemon_log_fp = fp
            stdout_target = fp
        except OSError:
            stdout_target = asyncio.subprocess.DEVNULL
        proc = await self._spawn(
            wireproxy_bin,
            "-c",
            self._path(WIREPROXY_CONFIG_FILE),
            stdout=stdout_target,
            stderr=asyncio.subprocess.STDOUT if stdout_target is not asyncio.subprocess.DEVNULL else asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=self.state_dir,
            env=build_warp_subprocess_env(),
        )
        self._daemon_proc = proc
        return proc, log_path

    async def _verify_trace_locked(self) -> dict[str, Any]:
        """Xác minh lưu lượng thực sự đi qua WARP bằng `cdn-cgi/trace` qua `socks5h://127.0.0.1:<port>` (không lưu IP thô)."""
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            text = await asyncio.wait_for(self._trace_fn(self.proxy_url), timeout=10.0)
            result = parse_trace_payload(text)
            result["error"] = None
        except Exception as exc:
            result = {
                "warp": "unknown",
                "warp_ok": False,
                "colo": None,
                "loc": None,
                "http": None,
                "error": sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}") or type(exc).__name__,
            }
        result["checked_at"] = checked_at
        self._trace = result
        return result

    async def start_and_connect(
        self,
        *,
        port: Optional[int] = None,
        force_reconnect: bool = False,
    ) -> dict[str, Any]:
        """Tạo hồ sơ `wgcf` (nếu thiếu), sinh `wireproxy.conf`, khởi chạy `wireproxy`, poll `127.0.0.1:<port>` và xác minh `cdn-cgi/trace`."""
        async with self._lock:
            port_changed = port is not None and int(port) != self.port
            if port is not None:
                self._set_loopback_port(port)
            self._enabled = True

            if not force_reconnect and not port_changed:
                already_up = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
                if already_up:
                    self._connected = True
                    self._mark_checked()
                    self._set_diagnostic("connected", None)
                    await self._apply_route_hooks(self.proxy_url)
                    return self.status_snapshot()

            if not self.is_installed():
                self._connected = False
                self._mark_checked()
                self._set_diagnostic("not_installed", self._not_installed_message())
                await self._apply_route_hooks(None)
                return self.status_snapshot()

            async def _fail(code: str, msg: Optional[str]) -> dict[str, Any]:
                self._connected = False
                self._mark_checked()
                self._set_diagnostic(code, msg)
                await self._apply_route_hooks(None)
                return self.status_snapshot()

            ok, msg, code = await self._ensure_profile_locked()
            if not ok:
                return await _fail(code, msg)

            await self._stop_wireproxy_locked(include_external=True)
            self._trace = None
            try:
                proc, log_path = await self._spawn_wireproxy_locked()
            except FileNotFoundError:
                return await _fail("not_installed", self._not_installed_message())
            except Exception as exc:
                return await _fail("daemon_not_running", f"wireproxy chưa chạy ({type(exc).__name__}: {exc})")

            poll_deadline = time.monotonic() + self._connect_poll_timeout
            reachable = False
            while True:
                if getattr(proc, "returncode", None) is not None:
                    tail = _read_sanitized_log_tail(log_path)
                    return await _fail("daemon_not_running", f"wireproxy thoát sớm ({tail})" if tail else "wireproxy thoát sớm")
                if await self._probe_async(self.host, self.port, self._probe_timeout):
                    reachable = True
                    break
                if time.monotonic() >= poll_deadline:
                    break
                await asyncio.sleep(self._connect_poll_interval)

            if not reachable:
                tail = _read_sanitized_log_tail(log_path)
                diag = f"127.0.0.1:{self.port} chưa lắng nghe ({tail})" if tail else f"127.0.0.1:{self.port} chưa phản hồi sau khi khởi chạy wireproxy"
                return await _fail("proxy_not_listening", diag)

            self._connected = True
            self._mark_checked()
            self._set_diagnostic("connected", None)
            await self._apply_route_hooks(self.proxy_url)
            if self._verify_trace_on_start:
                trace = await self._verify_trace_locked()
                if not trace.get("warp_ok"):
                    log.warning("wireproxy đã lắng nghe nhưng cdn-cgi/trace chưa xác nhận warp=on (%s).", trace.get("error") or trace.get("warp"))
            return self.status_snapshot()

    async def verify_warp_trace(self) -> dict[str, Any]:
        """Kiểm tra `https://www.cloudflare.com/cdn-cgi/trace` qua WARP proxy và trả về kết quả đã làm sạch (`warp=on/plus`, `colo`, `loc`)."""
        async with self._lock:
            reachable = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
            self._connected = reachable
            self._mark_checked()
            if not reachable:
                self._trace = {
                    "warp": "off",
                    "warp_ok": False,
                    "colo": None,
                    "loc": None,
                    "http": None,
                    "error": f"127.0.0.1:{self.port} chưa phản hồi",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
                return {"trace": dict(self._trace), "warp": self.status_snapshot()}
            trace = await self._verify_trace_locked()
            return {"trace": dict(trace), "warp": self.status_snapshot()}

    async def stop_and_disconnect(self) -> dict[str, Any]:
        """Tắt định tuyến Outbound WARP, dừng `wireproxy` (giải phóng RAM) và chuyển ngay Gemini + Discord sang Direct."""
        async with self._lock:
            self._enabled = False
            self._connected = False
            await self._apply_route_hooks(None)
            await self._stop_wireproxy_locked(include_external=True)
            self._trace = None
            self._mark_checked()
            self._set_diagnostic("disabled", None)
            return self.status_snapshot()

    async def shutdown(self) -> None:
        """Dừng an toàn tiến trình con `wireproxy` nếu do tiến trình Python trực tiếp khởi tạo."""
        async with self._lock:
            await self._stop_wireproxy_locked(include_external=False)

    async def refresh_status(self, *, force: bool = False) -> dict[str, Any]:
        """Kiểm tra tình trạng kết nối WARP proxy và RAM của `wireproxy` (có cache ngắn); tự động fallback/khôi phục khi cần."""
        now = time.monotonic()
        if not force and self._probed_once and (now - self._last_checked_mono) < self._cache_ttl:
            return self.status_snapshot()

        async with self._lock:
            now = time.monotonic()
            if not force and self._probed_once and (now - self._last_checked_mono) < self._cache_ttl:
                return self.status_snapshot()

            reachable = False
            probe_err: Optional[str] = None
            try:
                reachable = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
            except Exception as exc:
                probe_err = sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")
                reachable = False

            self._connected = reachable
            self._mark_checked()

            if reachable and self._enabled:
                self._set_diagnostic("connected", None)
            elif not self._enabled:
                self._set_diagnostic("disabled", None)
            elif probe_err:
                self._set_diagnostic("probe_error", probe_err)
            elif not self._diagnostic or self._diagnostic_code == "connected":
                code, msg = self._infer_offline_diagnostic()
                self._set_diagnostic(code, msg)

            desired_proxy = self.proxy_url if (self._enabled and self._connected) else None
            if desired_proxy != self._applied_proxy_url:
                if self._enabled and not self._connected and self._applied_proxy_url is not None:
                    log.warning("WARP proxy %s không phản hồi — tự động fallback sang Outbound Direct.", self.safe_proxy_url)
                await self._apply_route_hooks(desired_proxy)

            return self.status_snapshot()

    async def set_enabled(
        self,
        enabled: bool,
        *,
        force_probe: bool = True,
        bootstrap: bool = False,
        port: Optional[int] = None,
    ) -> dict[str, Any]:
        """Bật hoặc tắt định tuyến Outbound qua WARP ngay lập tức; khi `bootstrap=True` sẽ tự khởi chạy `wgcf` + `wireproxy` và poll kết nối."""
        if enabled and bootstrap:
            return await self.start_and_connect(port=port, force_reconnect=False)
        if not enabled and bootstrap:
            return await self.stop_and_disconnect()

        async with self._lock:
            if port is not None:
                self._set_loopback_port(port)
            self._enabled = bool(enabled)
            if force_probe:
                try:
                    self._connected = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
                except Exception as exc:
                    self._set_diagnostic("probe_error", f"{type(exc).__name__}: {exc}")
                    self._connected = False
                self._mark_checked()

            if self._enabled and self._connected:
                self._set_diagnostic("connected", None)
            elif not self._enabled:
                self._set_diagnostic("disabled", None)
            elif not self._diagnostic:
                code, msg = self._infer_offline_diagnostic()
                self._set_diagnostic(code, msg)

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
                self._set_diagnostic("runtime_fallback", reason)
            if was_using_proxy:
                log.warning("Phát hiện lỗi kết nối WARP proxy (%s) — lập tức chuyển sang Direct.", self._last_error or "unreachable")
                await self._apply_route_hooks(None)
            return was_using_proxy

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất telemetry gọn nhẹ về WARP (wireproxy) cho API `/api/status` và Dashboard (không chứa bí mật, khóa, IP thô hay đường dẫn nội bộ)."""
        active = self.is_active
        fallback_direct = bool(self._enabled and self._probed_once and not self._connected)
        return {
            "enabled": self._enabled,
            "connected": self._connected,
            "active": active,
            "fallback_direct": fallback_direct,
            "proxy_url": self.safe_proxy_url,
            "proxy_port": self.port,
            "outbound_mode": "proxy" if active else "direct",
            "engine": "wireproxy",
            "warp_installed": self.is_installed(),
            "warp_ram_mb": self._ram_mb,
            "warp_pid": self._pid,
            "trace": dict(self._trace) if (self._trace and self._enabled) else None,
            "diagnostic": None if (active or not self._enabled) else self._diagnostic,
            "diagnostic_code": "connected" if active else ("disabled" if not self._enabled else self._diagnostic_code),
            "last_checked_at": self._last_checked_iso,
            "last_error": None if (active or not self._enabled) else self._last_error,
        }
