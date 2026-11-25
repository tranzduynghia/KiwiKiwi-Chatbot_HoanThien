"""Tiện ích bảo mật: băm mật khẩu bằng scrypt và ký/xác thực cookie phiên đăng nhập."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any, Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

log = logging.getLogger("gemini-bot.security")


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
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_str),
            r=int(r_str),
            p=int(p_str),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


class SessionTokenSigner:
    """Bộ ký và giải mã token cookie an toàn cho phiên Dashboard và trạng thái OAuth2."""

    def __init__(self, secret_key: str = "", salt: str = "kiwikiwi-dashboard-v1"):
        cleaned_secret = (secret_key or "").strip()
        if not cleaned_secret:
            cleaned_secret = secrets.token_hex(32)
            log.warning(
                "Chưa đặt biến môi trường SESSION_SECRET — đang dùng khóa ngẫu nhiên tạm thời "
                "(phiên đăng nhập sẽ bị đăng xuất mỗi khi restart)."
            )
        self._serializer = URLSafeTimedSerializer(cleaned_secret, salt=salt)

    def encode(self, data: dict[str, Any]) -> str:
        """Ký dữ liệu từ điển thành chuỗi token an toàn cho cookie."""
        return self._serializer.dumps(data)

    def decode(self, token: Optional[str], max_age: int) -> Optional[dict[str, Any]]:
        """Xác thực chữ ký và hạn dùng của token cookie, trả về None nếu không hợp lệ."""
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=max_age)
            return data if isinstance(data, dict) else None
        except (BadSignature, SignatureExpired):
            return None
