"""Dịch vụ quản lý tiến trình `warp-svc`/`warp-cli` và định tuyến Outbound qua Cloudflare WARP User-Space Proxy (`127.0.0.1:40000`) kèm tự động fallback Direct."""
from __future__ import annotations

import asyncio
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
DEFAULT_WARP_PROXY_URL = f"http://{DEFAULT_WARP_PROXY_HOST}:{DEFAULT_WARP_PROXY_PORT}"
WARP_PROCESS_NAMES = frozenset({"warp-svc", "warp-svc.exe"})

# Các biến môi trường an toàn được phép truyền cho tiến trình con `warp-svc`/`warp-cli` (tuyệt đối không lộ bí mật bot)
_ALLOWED_WARP_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "DBUS_SYSTEM_BUS_ADDRESS",
)

# Gỡ đường dẫn hệ thống nội bộ khỏi thông điệp chẩn đoán trả về giao diện
_INTERNAL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._+-]+){2,}")


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


def sanitize_warp_diagnostic(raw_text: str, *, max_len: int = 180) -> str:
    """Làm sạch thông điệp stderr/status từ `warp-svc`/`warp-cli`: che toàn bộ bí mật và gỡ đường dẫn hệ thống nội bộ."""
    cleaned = redact_secrets(raw_text or "", max_len=max_len * 2)
    # Che cả chuỗi WARP_LICENSE_KEY hoặc WARP_CONNECTOR_TOKEN thực tế nếu vô tình xuất hiện trong stderr
    for env_name in ("WARP_LICENSE_KEY", "WARP_CONNECTOR_TOKEN"):
        secret_val = (os.getenv(env_name) or "").strip()
        if secret_val and len(secret_val) >= 4:
            cleaned = cleaned.replace(secret_val, "[REDACTED]")
    cleaned = _INTERNAL_PATH_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :;-")
    return cleaned[:max_len]


def build_warp_subprocess_env() -> dict[str, str]:
    """Tạo môi trường tối giản cho `warp-svc`/`warp-cli`, loại bỏ toàn bộ bí mật ứng dụng (DISCORD_TOKEN, GEMINI_API_KEY, WARP_LICENSE_KEY...)."""
    env = {
        key: os.environ[key]
        for key in _ALLOWED_WARP_ENV_KEYS
        if key in os.environ and os.environ[key]
    }
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
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


def _resolve_writable_log_path() -> str:
    """Chọn đường dẫn ghi log cho `warp-svc` đảm bảo luôn ghi được dưới user không có quyền root."""
    for directory in ("/var/log/cloudflare-warp", "/tmp"):
        try:
            os.makedirs(directory, exist_ok=True)
            if os.access(directory, os.W_OK):
                return os.path.join(directory, "warp-svc.log")
        except OSError:
            continue
    return "/tmp/warp-svc.log"


def _read_sanitized_log_tail(log_path: str, *, max_bytes: int = 2048) -> str:
    """Đọc dòng cuối cùng có nội dung của log `warp-svc` đã được làm sạch bí mật và đường dẫn."""
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
        # Ưu tiên dòng chứa ERROR / WARN / failed / denied gần nhất
        for line in reversed(lines[-10:]):
            lowered = line.lower()
            if any(tok in lowered for tok in ("error", "fail", "denied", "cannot", "unable", "panic", "refused")):
                return sanitize_warp_diagnostic(line)
        return sanitize_warp_diagnostic(lines[-1])
    except OSError:
        return ""


class WarpProxyService:
    """Quản lý vòng đời `warp-svc`/`warp-cli`, kiểm tra socket `127.0.0.1:<port>`, tự fallback Direct và đồng bộ nóng sang Gemini + Discord."""

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
        cache_ttl: float = 2.0,
        probe_timeout: float = 0.25,
        ipc_wait_timeout: float = 5.0,
        ipc_poll_interval: float = 0.25,
        connect_poll_timeout: float = 7.0,
        connect_poll_interval: float = 0.5,
        cli_timeout: float = 8.0,
        log_path: Optional[str] = None,
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
        self._which = which_fn or shutil.which
        self._spawn = spawn_subprocess or asyncio.create_subprocess_exec
        self._cache_ttl = max(0.01, float(cache_ttl))
        self._probe_timeout = max(0.05, float(probe_timeout))
        self._ipc_wait_timeout = max(0.2, float(ipc_wait_timeout))
        self._ipc_poll_interval = max(0.02, float(ipc_poll_interval))
        self._connect_poll_timeout = max(0.2, float(connect_poll_timeout))
        self._connect_poll_interval = max(0.02, float(connect_poll_interval))
        self._cli_timeout = max(0.5, float(cli_timeout))
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
        # Trước khi chạy kiểm tra mạng đầu tiên, tôn trọng OUTBOUND_PROXY_URL cấu hình tường minh
        return self.explicit_proxy_url

    def is_installed(self) -> bool:
        """Kiểm tra cả hai binary `warp-svc` và `warp-cli` có tồn tại trong container hay không."""
        return bool(self._which("warp-svc") and self._which("warp-cli"))

    def effective_proxy_url(self) -> str:
        """Trả về URL proxy hiệu lực (`http://127.0.0.1:40000`) nếu WARP đang bật & sẵn sàng, ngược lại trả về `""` (Direct)."""
        return self.proxy_url if self.is_active else ""

    def register_route_hook(self, hook: Callable[[Optional[str]], Any]) -> None:
        """Đăng ký callback nhận `proxy_url | None` mỗi khi tuyến Outbound chuyển giữa WARP và Direct."""
        self._route_hooks.append(hook)

    def _set_loopback_port(self, port: int) -> None:
        """Cập nhật cổng proxy cục bộ, luôn khóa chặt về loopback `127.0.0.1:<port>` (không bao giờ mở ra ngoài)."""
        port_int = int(port)
        if not (1 <= port_int <= 65535):
            raise ValueError("Cổng WARP proxy phải nằm trong khoảng 1..65535.")
        self.host = DEFAULT_WARP_PROXY_HOST
        self.port = port_int
        self.proxy_url = f"http://{DEFAULT_WARP_PROXY_HOST}:{port_int}"
        self.safe_proxy_url = self.proxy_url
        self.explicit_proxy_url = False

    def _set_diagnostic(self, code: str, message: Optional[str]) -> None:
        """Cập nhật mã và thông điệp chẩn đoán đã làm sạch cho thẻ WARP trên Dashboard."""
        self._diagnostic_code = code
        self._diagnostic = sanitize_warp_diagnostic(message) if message else None
        self._last_error = self._diagnostic

    def _infer_offline_diagnostic(self) -> tuple[str, str]:
        """Suy luận chẩn đoán rõ ràng khi WARP đang bật nhưng cổng loopback chưa phản hồi."""
        if not self.is_installed():
            return "not_installed", "Chưa cài cloudflare-warp"
        pid, _ram = self._metrics_fn()
        daemon_alive = (
            self._daemon_proc is not None and getattr(self._daemon_proc, "returncode", None) is None
        ) or (pid is not None)
        if not daemon_alive:
            log_path = self._log_path or "/var/log/cloudflare-warp/warp-svc.log"
            tail = _read_sanitized_log_tail(log_path) or _read_sanitized_log_tail("/tmp/warp-svc.log")
            if tail:
                return "daemon_not_running", f"warp-svc chưa khởi động ({tail})"
            return "daemon_not_running", "warp-svc chưa khởi động"
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
        self._probed_once = True
        self._last_checked_mono = time.monotonic()
        self._last_checked_iso = datetime.now(timezone.utc).isoformat()
        self._pid, self._ram_mb = self._metrics_fn()
        self._applied_proxy_url = self.proxy_url if reachable else None
        if reachable:
            self._set_diagnostic("connected", None)
            log.info("Đã phát hiện Cloudflare WARP proxy sẵn sàng tại %s (RAM: %s MB).", self.safe_proxy_url, self._ram_mb)
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

    async def _run_warp_cli(self, *args: str, timeout: Optional[float] = None) -> tuple[int, str, str]:
        """Chạy `warp-cli --accept-tos ...` an toàn (`shell=False`, tách biệt env bí mật, giới hạn timeout)."""
        warp_cli = self._which("warp-cli")
        if not warp_cli:
            return 127, "", "Chưa cài cloudflare-warp"
        cmd = (warp_cli, "--accept-tos", *args)
        try:
            proc = await self._spawn(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=build_warp_subprocess_env(),
            )
        except FileNotFoundError:
            return 127, "", "Chưa cài cloudflare-warp"
        except Exception as exc:
            return 1, "", sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout if timeout is not None else self._cli_timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return 124, "", f"warp-cli {args[0] if args else ''} quá hạn thời gian"
        except Exception as exc:
            return 1, "", sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")

        stdout_text = (stdout_b or b"").decode("utf-8", errors="replace").strip()
        stderr_text = (stderr_b or b"").decode("utf-8", errors="replace").strip()
        rc = getattr(proc, "returncode", 0)
        return int(rc if rc is not None else 0), stdout_text, stderr_text

    def _close_daemon_log_handle(self) -> None:
        """Đóng file handle log của `warp-svc` an toàn nếu đang mở."""
        fp = self._daemon_log_fp
        self._daemon_log_fp = None
        if fp is not None:
            try:
                fp.close()
            except Exception:
                pass

    async def _spawn_warp_daemon(self, warp_svc_bin: str, *, accept_tos: bool) -> tuple[Any, str]:
        """Khởi chạy nền `warp-svc` với luồng log chuyển hướng an toàn không phụ thuộc systemd/dbus."""
        for runtime_dir in ("/var/lib/cloudflare-warp", "/run/cloudflare-warp"):
            try:
                os.makedirs(runtime_dir, exist_ok=True)
            except OSError:
                pass

        log_path = self._log_path or _resolve_writable_log_path()
        self._log_path = log_path
        self._close_daemon_log_handle()

        stdout_target: Any = asyncio.subprocess.DEVNULL
        stderr_target: Any = asyncio.subprocess.PIPE
        try:
            fp = open(log_path, "ab")
            self._daemon_log_fp = fp
            stdout_target = fp
            stderr_target = fp
        except OSError:
            stdout_target = asyncio.subprocess.DEVNULL
            stderr_target = asyncio.subprocess.PIPE

        cmd = (warp_svc_bin, "--accept-tos") if accept_tos else (warp_svc_bin,)
        proc = await self._spawn(
            *cmd,
            stdout=stdout_target,
            stderr=stderr_target,
            env=build_warp_subprocess_env(),
        )
        self._daemon_proc = proc
        return proc, log_path

    async def _read_proc_exit_reason(self, proc: Any, log_path: str) -> str:
        """Lấy thông điệp lỗi đã làm sạch khi tiến trình `warp-svc` thoát sớm."""
        stderr_obj = getattr(proc, "stderr", None)
        if stderr_obj is not None and hasattr(stderr_obj, "read"):
            try:
                data = await asyncio.wait_for(stderr_obj.read(), timeout=0.3)
                if data:
                    text = sanitize_warp_diagnostic(data.decode("utf-8", errors="replace"))
                    if text:
                        return text
            except Exception:
                pass
        return _read_sanitized_log_tail(log_path)

    async def _ensure_daemon_ready_locked(self) -> tuple[bool, Optional[str], str]:
        """Đảm bảo `warp-svc` đang chạy và IPC socket phản hồi trong tối đa 5 giây (gọi bên trong `self._lock`)."""
        # 1) Kiểm tra nhanh IPC xem daemon có sẵn sàng rồi hay không
        rc, _out, _err = await self._run_warp_cli("status", timeout=min(3.0, self._cli_timeout))
        if rc == 0:
            return True, None, "connected"

        warp_svc_bin = self._which("warp-svc")
        if not warp_svc_bin:
            return False, "Chưa cài cloudflare-warp", "not_installed"

        # 2) Nếu chưa có tiến trình `warp-svc` đang chạy -> khởi chạy nền
        pid, _ram = self._metrics_fn()
        managed_alive = (
            self._daemon_proc is not None and getattr(self._daemon_proc, "returncode", None) is None
        )
        log_path = self._log_path
        if not managed_alive and pid is None:
            try:
                proc, log_path = await self._spawn_warp_daemon(warp_svc_bin, accept_tos=True)
            except FileNotFoundError:
                return False, "Chưa cài cloudflare-warp", "not_installed"
            except Exception as exc:
                detail = sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")
                return False, f"warp-svc chưa khởi động ({detail})", "daemon_not_running"

            await asyncio.sleep(min(0.25, self._ipc_poll_interval))
            if getattr(proc, "returncode", None) is not None:
                early_err = await self._read_proc_exit_reason(proc, log_path)
                if any(tok in early_err.lower() for tok in ("unexpected argument", "unrecognized", "unknown option", "accept-tos")):
                    try:
                        proc, log_path = await self._spawn_warp_daemon(warp_svc_bin, accept_tos=False)
                    except Exception as exc:
                        detail = sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")
                        return False, f"warp-svc chưa khởi động ({detail})", "daemon_not_running"
                else:
                    msg = f"warp-svc chưa khởi động ({early_err})" if early_err else "warp-svc chưa khởi động"
                    return False, msg, "daemon_not_running"

        # 3) Chờ IPC socket sẵn sàng trong 3–5 giây
        deadline = time.monotonic() + self._ipc_wait_timeout
        while True:
            if self._daemon_proc is not None and getattr(self._daemon_proc, "returncode", None) is not None:
                reason = await self._read_proc_exit_reason(self._daemon_proc, log_path)
                msg = f"warp-svc chưa khởi động ({reason})" if reason else "warp-svc chưa khởi động"
                return False, msg, "daemon_not_running"

            rc, _out, err = await self._run_warp_cli("status", timeout=min(2.5, self._cli_timeout))
            if rc == 0:
                return True, None, "ipc_ready"

            if time.monotonic() >= deadline:
                detail = sanitize_warp_diagnostic(err) or _read_sanitized_log_tail(log_path)
                if detail:
                    return False, f"warp-svc chưa khởi động ({detail})", "daemon_not_running"
                return False, "warp-svc chưa khởi động (IPC chưa sẵn sàng)", "daemon_not_running"

            await asyncio.sleep(self._ipc_poll_interval)

    async def _configure_and_connect_locked(self) -> tuple[bool, Optional[str], str]:
        """Đăng ký profile WARP (nếu thiếu), nạp license key tùy chọn, ép chế độ proxy loopback và gọi `connect`."""
        # 1) Kiểm tra hồ sơ đăng ký hiện tại
        reg_rc, reg_out, reg_err = await self._run_warp_cli("registration", "show")
        reg_combined = f"{reg_out} {reg_err}".lower()
        needs_registration = reg_rc != 0 or any(
            phrase in reg_combined for phrase in ("missing registration", "no registration", "not registered")
        )

        connector_token = (os.getenv("WARP_CONNECTOR_TOKEN") or "").strip()
        license_key = (os.getenv("WARP_LICENSE_KEY") or "").strip()

        if needs_registration:
            if connector_token:
                c_rc, c_out, c_err = await self._run_warp_cli("connector", "new", connector_token)
                if c_rc != 0:
                    detail = sanitize_warp_diagnostic(c_err or c_out)
                    msg = f"Đăng ký WARP Connector thất bại ({detail})" if detail else "Đăng ký WARP Connector thất bại"
                    return False, msg, "registration_failed"
            else:
                n_rc, n_out, n_err = await self._run_warp_cli("registration", "new")
                if n_rc != 0:
                    detail = sanitize_warp_diagnostic(n_err or n_out)
                    msg = f"Đăng ký hồ sơ WARP thất bại ({detail})" if detail else "Đăng ký hồ sơ WARP thất bại"
                    return False, msg, "registration_failed"

        if license_key and not connector_token:
            l_rc, l_out, l_err = await self._run_warp_cli("registration", "license", license_key)
            if l_rc != 0:
                # Không làm hỏng quá trình kết nối nếu license đã gắn hoặc key bị từ chối tạm thời, chỉ ghi cảnh báo đã che bí mật
                log.warning(
                    "Không áp dụng được WARP_LICENSE_KEY: %s",
                    sanitize_warp_diagnostic(l_err or l_out) or "rejected",
                )

        # 2) Ép chế độ user-space proxy, đặt cổng loopback và kết nối
        m_rc, m_out, m_err = await self._run_warp_cli("mode", "proxy")
        if m_rc != 0:
            detail = sanitize_warp_diagnostic(m_err or m_out)
            msg = f"Không đặt được WARP mode proxy ({detail})" if detail else "Không đặt được WARP mode proxy"
            return False, msg, "mode_proxy_failed"

        p_rc, p_out, p_err = await self._run_warp_cli("proxy", "port", str(self.port))
        if p_rc != 0:
            detail = sanitize_warp_diagnostic(p_err or p_out)
            msg = f"Không đặt được cổng WARP proxy {self.port} ({detail})" if detail else f"Không đặt được cổng WARP proxy {self.port}"
            return False, msg, "proxy_port_failed"

        conn_rc, conn_out, conn_err = await self._run_warp_cli("connect")
        if conn_rc != 0:
            detail = sanitize_warp_diagnostic(conn_err or conn_out)
            msg = f"Lệnh warp-cli connect thất bại ({detail})" if detail else "Lệnh warp-cli connect thất bại"
            return False, msg, "connect_failed"

        return True, None, "connecting"

    async def start_and_connect(
        self,
        *,
        port: Optional[int] = None,
        force_reconnect: bool = False,
    ) -> dict[str, Any]:
        """Khởi động `warp-svc` (nếu cần), đăng ký/cấu hình proxy loopback, gọi `connect` và poll `127.0.0.1:<port>` 6–8s."""
        async with self._lock:
            if port is not None:
                self._set_loopback_port(port)
            self._enabled = True

            # Nhánh nhanh: nếu cổng loopback đã lắng nghe sẵn và không yêu cầu cấu hình lại
            if not force_reconnect:
                already_up = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
                if already_up:
                    self._connected = True
                    self._probed_once = True
                    self._last_checked_mono = time.monotonic()
                    self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                    self._pid, self._ram_mb = self._metrics_fn()
                    self._set_diagnostic("connected", None)
                    await self._apply_route_hooks(self.proxy_url)
                    return self.status_snapshot()

            # Kiểm tra cài đặt binary trước khi thao tác
            if not self.is_installed():
                self._connected = False
                self._probed_once = True
                self._last_checked_mono = time.monotonic()
                self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                self._pid, self._ram_mb = self._metrics_fn()
                self._set_diagnostic("not_installed", "Chưa cài cloudflare-warp")
                await self._apply_route_hooks(None)
                return self.status_snapshot()

            # Đảm bảo daemon `warp-svc` chạy và IPC sẵn sàng (3–5s)
            daemon_ok, daemon_msg, daemon_code = await self._ensure_daemon_ready_locked()
            if not daemon_ok:
                self._connected = False
                self._probed_once = True
                self._last_checked_mono = time.monotonic()
                self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                self._pid, self._ram_mb = self._metrics_fn()
                self._set_diagnostic(daemon_code, daemon_msg)
                await self._apply_route_hooks(None)
                return self.status_snapshot()

            # Đăng ký (nếu thiếu), đặt mode proxy + proxy port + connect
            cfg_ok, cfg_msg, cfg_code = await self._configure_and_connect_locked()
            if not cfg_ok:
                self._connected = False
                self._probed_once = True
                self._last_checked_mono = time.monotonic()
                self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                self._pid, self._ram_mb = self._metrics_fn()
                self._set_diagnostic(cfg_code, cfg_msg)
                await self._apply_route_hooks(None)
                return self.status_snapshot()

            # Poll chủ động socket 127.0.0.1:<port> trong 6–8 giây (mỗi 0.5s) trước khi trả về Frontend
            poll_deadline = time.monotonic() + self._connect_poll_timeout
            reachable = False
            while True:
                if await self._probe_async(self.host, self.port, self._probe_timeout):
                    reachable = True
                    break
                if time.monotonic() >= poll_deadline:
                    break
                await asyncio.sleep(self._connect_poll_interval)

            self._connected = reachable
            self._probed_once = True
            self._last_checked_mono = time.monotonic()
            self._last_checked_iso = datetime.now(timezone.utc).isoformat()
            self._pid, self._ram_mb = self._metrics_fn()

            if reachable:
                self._set_diagnostic("connected", None)
                await self._apply_route_hooks(self.proxy_url)
                return self.status_snapshot()

            # Thu thập trạng thái thực tế từ `warp-cli status` để trả về chẩn đoán rõ ràng thay vì timeout chung chung
            st_rc, st_out, st_err = await self._run_warp_cli("status", timeout=min(3.0, self._cli_timeout))
            status_detail = sanitize_warp_diagnostic(st_out or st_err)
            if not status_detail and self._log_path:
                status_detail = _read_sanitized_log_tail(self._log_path)
            if status_detail:
                diag_msg = f"127.0.0.1:{self.port} chưa lắng nghe ({status_detail})"
            else:
                diag_msg = f"127.0.0.1:{self.port} chưa phản hồi sau khi gọi warp-cli connect"
            self._set_diagnostic("proxy_not_listening", diag_msg)
            await self._apply_route_hooks(None)
            return self.status_snapshot()

    async def stop_and_disconnect(self) -> dict[str, Any]:
        """Tắt định tuyến Outbound WARP, ngắt kết nối `warp-cli` (best-effort) và chuyển ngay Gemini + Discord sang Direct."""
        async with self._lock:
            self._enabled = False
            self._connected = False
            self._probed_once = True
            self._last_checked_mono = time.monotonic()
            self._last_checked_iso = datetime.now(timezone.utc).isoformat()

            await self._apply_route_hooks(None)

            if self._which("warp-cli"):
                pid, _ram = self._metrics_fn()
                managed_alive = (
                    self._daemon_proc is not None and getattr(self._daemon_proc, "returncode", None) is None
                )
                if managed_alive or pid is not None:
                    await self._run_warp_cli("disconnect", timeout=min(3.0, self._cli_timeout))

            self._pid, self._ram_mb = self._metrics_fn()
            self._set_diagnostic("disabled", None)
            return self.status_snapshot()

    async def shutdown(self) -> None:
        """Dừng an toàn tiến trình con `warp-svc` nếu do tiến trình Python trực tiếp khởi tạo."""
        async with self._lock:
            proc = self._daemon_proc
            self._daemon_proc = None
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
            probe_err: Optional[str] = None
            try:
                reachable = bool(await self._probe_async(self.host, self.port, self._probe_timeout))
            except Exception as exc:
                probe_err = sanitize_warp_diagnostic(f"{type(exc).__name__}: {exc}")
                reachable = False

            self._connected = reachable
            self._probed_once = True
            self._last_checked_mono = time.monotonic()
            self._last_checked_iso = datetime.now(timezone.utc).isoformat()
            self._pid, self._ram_mb = self._metrics_fn()

            if reachable and self._enabled:
                self._set_diagnostic("connected", None)
            elif not self._enabled:
                self._set_diagnostic("disabled", None)
            elif probe_err:
                self._set_diagnostic("probe_error", probe_err)
            elif not self._diagnostic:
                code, msg = self._infer_offline_diagnostic()
                self._set_diagnostic(code, msg)

            desired_proxy = self.proxy_url if (self._enabled and self._connected) else None
            if desired_proxy != self._applied_proxy_url:
                if self._enabled and not self._connected and self._applied_proxy_url is not None:
                    log.warning(
                        "WARP proxy %s không phản hồi — tự động fallback sang Outbound Direct.",
                        self.safe_proxy_url,
                    )
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
        """Bật hoặc tắt định tuyến Outbound qua Cloudflare WARP ngay lập tức; khi `bootstrap=True` sẽ tự khởi chạy `warp-svc` và poll kết nối."""
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
                self._probed_once = True
                self._last_checked_mono = time.monotonic()
                self._last_checked_iso = datetime.now(timezone.utc).isoformat()
                self._pid, self._ram_mb = self._metrics_fn()

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
                log.warning(
                    "Phát hiện lỗi kết nối WARP proxy (%s) — lập tức chuyển sang Direct.",
                    self._last_error or "unreachable",
                )
                await self._apply_route_hooks(None)
            return was_using_proxy

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất telemetry gọn nhẹ về Cloudflare WARP cho API `/api/status` và Dashboard (không chứa bí mật hay đường dẫn nội bộ)."""
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
            "warp_installed": self.is_installed(),
            "warp_ram_mb": self._ram_mb,
            "warp_pid": self._pid,
            "diagnostic": None if (active or not self._enabled) else self._diagnostic,
            "diagnostic_code": "connected" if active else ("disabled" if not self._enabled else self._diagnostic_code),
            "last_checked_at": self._last_checked_iso,
            "last_error": None if (active or not self._enabled) else self._last_error,
        }
