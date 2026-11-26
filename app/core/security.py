"""Tiện ích bảo mật lõi: băm scrypt, ký cookie HMAC-SHA256, chống rò rỉ bí mật, kiểm tra proxy tin cậy và rate limiter."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from typing import Any, Optional
from urllib.parse import urlparse

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.settings import SENSITIVE_KEY_TOKENS

log = logging.getLogger("gemini-bot.security")

STATIC_SECRET_PLACEHOLDER = "••••••••••••"
MIN_RECOMMENDED_SECRET_LEN = 32

# Giới hạn số phép băm scrypt chạy song song (mỗi phép dùng ~16MB RAM trên container 488MB)
_SCRYPT_SEMAPHORE = asyncio.Semaphore(2)

# Các khóa an toàn (cờ boolean hoặc bộ đếm số học) không chứa giá trị bí mật
SAFE_NON_SECRET_KEYS = frozenset({
    "has_cf_tunnel_token",
    "cf_tunnel_has_token",
    "outbound_proxy_configured",
    "discord_oauth_enabled",
    "tokens",
    "total_token_count",
})

# Chỉ cho phép ký tự an toàn của Cloudflare Tunnel Token (JWT/Base64url), chặn bắt đầu bằng '-' (flag injection)
CF_TUNNEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{7,4095}$")

# Mẫu nhận diện chuỗi bí mật/hash để che sạch khỏi log & thông báo lỗi API
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"mongodb(?:\+srv)?://[^\s'\"<>]+", re.IGNORECASE), "mongodb://[REDACTED]"),
    (re.compile(r"(https?://)[^:\s/@'\"]+:[^@\s/'\"]+@", re.IGNORECASE), r"\1[REDACTED]@"),
    (re.compile(r"scrypt\$\d+\$\d+\$\d+\$[0-9a-fA-F]+\$[0-9a-fA-F]+"), "[REDACTED_HASH]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9._-]{8,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"[MN][A-Za-z\d]{23,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"), "[REDACTED_TOKEN]"),
    (
        re.compile(
            r"((?:api[_-]?key|token|secret|password|passwd|auth|key)\s*[=:]\s*)([^\s&'\",;]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
)

_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::ffff:127.0.0.0/104"),
)


def hash_password(password: str) -> str:
    """Băm mật khẩu với thuật toán scrypt kèm salt ngẫu nhiên 16 byte."""
    salt = secrets.token_bytes(16)
    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${derived_key.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Kiểm tra mật khẩu thô với chuỗi băm scrypt theo thời gian hằng (chống timing attack)."""
    try:
        parts = stored_hash.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        _, n_str, r_str, p_str, salt_hex, dk_hex = parts
        n_val, r_val, p_val = int(n_str), int(r_str), int(p_str)
        # Chống DoS nếu bản ghi hash bị sửa tham số chi phí quá lớn
        if n_val <= 0 or n_val > 2**17 or r_val <= 0 or r_val > 16 or p_val <= 0 or p_val > 4:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n_val,
            r=r_val,
            p=p_val,
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


async def hash_password_async(password: str) -> str:
    """Băm mật khẩu trên luồng phụ với giới hạn đồng thời để không chặn event loop hoặc gây tràn RAM 488MB."""
    async with _SCRYPT_SEMAPHORE:
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored_hash: str) -> bool:
    """Xác thực mật khẩu trên luồng phụ với giới hạn đồng thời an toàn cho container 488MB."""
    async with _SCRYPT_SEMAPHORE:
        return await asyncio.to_thread(verify_password, password, stored_hash)


def is_sensitive_key(field_path: str) -> bool:
    """Kiểm tra tên trường có thuộc nhóm bí mật hoặc chuỗi băm xác thực hay không."""
    cleaned = str(field_path or "").strip().lower()
    if not cleaned:
        return False
    leaf = cleaned.rsplit(".", 1)[-1]
    if leaf in SAFE_NON_SECRET_KEYS:
        return False
    return any(token in cleaned for token in SENSITIVE_KEY_TOKENS)


def redact_secrets(text: str, max_len: int = 240) -> str:
    """Che toàn bộ URI chứa mật khẩu, API key, JWT/Tunnel token, Discord token và password hash khỏi chuỗi lỗi."""
    cleaned = str(text or "")
    for pattern, replacement in _REDACT_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = cleaned.replace("\r", " ").replace("\n", " ").strip()
    return cleaned[:max_len]


def _strip_sensitive_recursive(obj: Any, prefix: str = "") -> Any:
    """Loại bỏ đệ quy mọi khóa bí mật hoặc chuỗi băm ra khỏi từ điển/danh sách trước khi trả về API."""
    if isinstance(obj, dict):
        cleaned: dict[str, Any] = {}
        for key, value in obj.items():
            key_str = str(key)
            path = f"{prefix}.{key_str}" if prefix else key_str
            if is_sensitive_key(path):
                continue
            cleaned[key_str] = _strip_sensitive_recursive(value, path)
        return cleaned
    if isinstance(obj, list):
        return [_strip_sensitive_recursive(item, prefix) for item in obj]
    return obj


def sanitize_config_for_response(cfg: dict[str, Any]) -> dict[str, Any]:
    """Thực thi Zero-Secret & Zero-Hash cho payload `/api/config*`: loại bỏ hoàn toàn `cf_tunnel_token` và mọi bí mật/hash, chỉ trả cờ boolean."""
    raw = copy.deepcopy(cfg or {})
    root_token = str(raw.pop("cf_tunnel_token", "") or "").strip()
    tunnel_raw = raw.get("tunnel") if isinstance(raw.get("tunnel"), dict) else None
    has_token = bool(root_token)
    if tunnel_raw is not None:
        has_token = bool(str(tunnel_raw.get("cf_tunnel_token") or "").strip() or root_token)

    safe = _strip_sensitive_recursive(raw)
    if isinstance(safe.get("tunnel"), dict):
        safe["tunnel"]["has_cf_tunnel_token"] = has_token
        safe["tunnel"]["cf_tunnel_has_token"] = has_token
    return safe


def validate_cloudflare_tunnel_token(token: str) -> str:
    """Xác thực chặt chẽ định dạng Cloudflare Tunnel Token, chặn flag injection (`--...`), khoảng trắng và ký tự điều khiển."""
    cleaned = str(token or "").strip()
    if not cleaned:
        return ""
    if not CF_TUNNEL_TOKEN_RE.fullmatch(cleaned):
        raise ValueError(
            "Cloudflare Tunnel Token không hợp lệ (chỉ chấp nhận ký tự Base64/JWT an toàn, không chứa khoảng trắng hoặc dấu '-' ở đầu)."
        )
    return cleaned


def sanitize_https_url(url: str) -> str:
    """Chỉ cho phép URL dùng giao thức `https://` hợp lệ để chống chèn `javascript:` hoặc `data:` vào thuộc tính HTML."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return ""
    return raw


def _parse_trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Đọc danh sách IP/CIDR reverse proxy tin cậy từ biến môi trường `TRUSTED_PROXY_IPS` (mặc định chỉ loopback)."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = list(_LOOPBACK_NETWORKS)
    raw = (os.getenv("TRUSTED_PROXY_IPS") or "").strip()
    if not raw:
        return networks
    for entry in re.split(r"[,\s]+", raw):
        item = entry.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning("Bỏ qua mục TRUSTED_PROXY_IPS không hợp lệ: %r", item)
    return networks


def is_trusted_proxy_ip(host: Optional[str]) -> bool:
    """Xác định kết nối TCP đến từ loopback cục bộ (`cloudflared` trong container) hoặc dải `TRUSTED_PROXY_IPS`."""
    if not host:
        return False
    cleaned = str(host).strip()
    # Starlette TestClient dùng peer ảo "testclient" (không thể xảy ra trên socket TCP thật của Uvicorn)
    if cleaned == "testclient":
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        return False
    for net in _parse_trusted_proxy_networks():
        if ip.version == net.version and ip in net:
            return True
    return False


class SlidingWindowRateLimiter:
    """Bộ giới hạn tần suất cửa sổ trượt có trần bộ nhớ cứng (chống brute-force & OOM trên container 488MB)."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        window_seconds: float = 60.0,
        max_keys: int = 2048,
    ):
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(64, int(max_keys))
        self._buckets: OrderedDict[str, deque[float]] = OrderedDict()

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._buckets.get(key)
        if bucket is None:
            return deque()
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            self._buckets.pop(key, None)
            return deque()
        self._buckets.move_to_end(key)
        return bucket

    def check(self, key: str) -> tuple[bool, int]:
        """Kiểm tra khóa hiện có đang bị chặn hay không; trả về `(allowed, retry_after_seconds)`."""
        now = time.monotonic()
        bucket = self._prune(key, now)
        if len(bucket) >= self.max_attempts:
            retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
            return False, retry_after
        return True, 0

    def record_failure(self, key: str) -> None:
        """Ghi nhận một lần xác thực thất bại cho khóa và tự thu hồi mục cũ nhất nếu vượt `max_keys`."""
        now = time.monotonic()
        bucket = self._prune(key, now)
        bucket.append(now)
        self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)

    def reset(self, key: str) -> None:
        """Xóa lịch sử thất bại của khóa sau khi đăng nhập thành công."""
        self._buckets.pop(key, None)


class SessionTokenSigner:
    """Bộ ký và giải mã token cookie an toàn (HMAC-SHA256) cho phiên Dashboard và trạng thái OAuth2."""

    def __init__(self, secret_key: str = "", salt: str = "kiwikiwi-dashboard-v1"):
        cleaned_secret = (secret_key or "").strip()
        if not cleaned_secret:
            cleaned_secret = secrets.token_hex(32)
            log.warning(
                "Chưa đặt biến môi trường SESSION_SECRET — đang dùng khóa ngẫu nhiên tạm thời 256-bit "
                "(phiên đăng nhập sẽ bị đăng xuất mỗi khi restart)."
            )
        elif len(cleaned_secret) < MIN_RECOMMENDED_SECRET_LEN:
            log.warning(
                "Biến môi trường SESSION_SECRET quá ngắn (%d ký tự, khuyến nghị >= %d ký tự ngẫu nhiên).",
                len(cleaned_secret),
                MIN_RECOMMENDED_SECRET_LEN,
            )
        self._serializer = URLSafeTimedSerializer(
            cleaned_secret,
            salt=salt,
            signer_kwargs={"digest_method": hashlib.sha256},
        )

    def encode(self, data: dict[str, Any]) -> str:
        """Ký dữ liệu từ điển thành chuỗi token an toàn cho cookie."""
        return self._serializer.dumps(data)

    def decode(self, token: Optional[str], max_age: int) -> Optional[dict[str, Any]]:
        """Xác thực chữ ký và hạn dùng của token cookie, trả về None nếu không hợp lệ."""
        if not token or not isinstance(token, str):
            return None
        try:
            data = self._serializer.loads(token, max_age=max_age)
            return data if isinstance(data, dict) else None
        except (BadSignature, SignatureExpired):
            return None
