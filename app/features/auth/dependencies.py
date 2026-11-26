"""Dependency xác thực phiên đăng nhập FastAPI và tiện ích cookie cho Dashboard."""
from __future__ import annotations

import hmac
import os
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.security import SessionTokenSigner, is_trusted_proxy_ip, sanitize_https_url
from app.features.permissions.service import PermissionService

SESSION_COOKIE_NAME = "kiwikiwi_session"
OAUTH_STATE_COOKIE_NAME = "kiwikiwi_oauth_state"
DEFAULT_SESSION_MAX_AGE = 60 * 60 * 24  # 24 giờ (mặc định an toàn)
OAUTH_STATE_MAX_AGE = 300  # 5 phút


def _resolve_session_max_age() -> int:
    """Đọc thời hạn phiên từ `SESSION_MAX_AGE_SECONDS` và kẹp trong khoảng 5 phút..7 ngày."""
    raw = (os.getenv("SESSION_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_SESSION_MAX_AGE
    try:
        parsed = int(raw)
        return max(300, min(60 * 60 * 24 * 7, parsed))
    except ValueError:
        return DEFAULT_SESSION_MAX_AGE


SESSION_MAX_AGE = _resolve_session_max_age()


class AuthDependencies:
    """Cung cấp các dependency FastAPI (`require_user`, `require_admin`) và xử lý cookie phiên."""

    def __init__(
        self,
        signer: SessionTokenSigner,
        permission_service: PermissionService,
        user_repo: Any = None,
    ):
        self.signer = signer
        self._permissions = permission_service
        self._users = user_repo

    @staticmethod
    def is_secure_request(request: Request) -> bool:
        """Xác định request đang chạy qua HTTPS (chỉ tin `X-Forwarded-Proto` từ reverse proxy tin cậy)."""
        public_url = (os.getenv("DASHBOARD_PUBLIC_URL") or "").strip().lower()
        if public_url.startswith("https://"):
            return True
        if (request.url.scheme or "").lower() == "https":
            return True
        peer_host = request.client.host if request.client else None
        if is_trusted_proxy_ip(peer_host):
            forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if forwarded_proto == "https":
                return True
        return False

    @staticmethod
    def public_user_payload(session_data: dict[str, Any]) -> dict[str, Any]:
        """Lọc dữ liệu phiên trước khi trả về `/api/me` hoặc `/auth/login`, đảm bảo không lộ trường nội bộ/hash."""
        allowed_keys = (
            "auth_type",
            "username",
            "display_name",
            "discord_id",
            "avatar_url",
            "role",
            "is_admin",
            "login_at",
        )
        out = {k: session_data[k] for k in allowed_keys if k in session_data}
        if "avatar_url" in out:
            out["avatar_url"] = sanitize_https_url(str(out.get("avatar_url") or ""))
        return out

    @staticmethod
    def actor_label(user: dict[str, Any]) -> tuple[str, str]:
        """Tạo nhãn người thao tác và loại xác thực phục vụ Audit Log."""
        if user.get("auth_type") == "discord":
            display = user.get("display_name") or user.get("username")
            return f"{display} (discord:{user.get('discord_id')})", "discord"
        return str(user.get("username") or "dashboard"), "password"

    @staticmethod
    def actor_meta(user: dict[str, Any]) -> dict[str, str]:
        """Trích xuất siêu dữ liệu hiển thị (avatar, tên hiển thị, Discord ID) của người thao tác."""
        if user.get("auth_type") == "discord":
            return {
                "actor_display_name": str(user.get("display_name") or user.get("username") or "Discord User"),
                "actor_discord_id": str(user.get("discord_id") or ""),
                "actor_avatar_url": sanitize_https_url(str(user.get("avatar_url") or "")),
            }
        return {
            "actor_display_name": str(user.get("display_name") or user.get("username") or "dashboard"),
            "actor_discord_id": "",
            "actor_avatar_url": "",
        }

    def set_session_cookie(self, response: Response, request: Request, session_data: dict[str, Any]) -> None:
        """Gắn cookie phiên đã ký bảo mật (HttpOnly, SameSite=Lax, Secure khi HTTPS) vào HTTP response."""
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=self.signer.encode(session_data),
            max_age=SESSION_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self.is_secure_request(request),
        )

    def clear_session_cookie(self, response: Response, request: Optional[Request] = None) -> None:
        """Xóa cookie phiên đăng nhập khỏi trình duyệt với đúng thuộc tính path/samesite/httponly."""
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self.is_secure_request(request) if request is not None else False,
        )

    async def get_current_user(self, request: Request) -> Optional[dict[str, Any]]:
        """Giải mã phiên hiện tại và xác thực lại trạng thái tài khoản/quyền trực tiếp (thu hồi tức thì khi xóa hoặc đổi mật khẩu)."""
        session_data = self.signer.decode(request.cookies.get(SESSION_COOKIE_NAME), max_age=SESSION_MAX_AGE)
        if not session_data:
            return None

        auth_type = session_data.get("auth_type")
        if auth_type == "discord":
            uid = str(session_data.get("discord_id") or "").strip()
            if not uid or not uid.isdigit():
                return None
            allowed, is_admin = await self._permissions.can_access_dashboard(uid)
            if not allowed:
                return None
            session_data["is_admin"] = is_admin
            session_data["role"] = "admin" if is_admin else "operator"
            return self.public_user_payload(session_data)

        if auth_type == "password":
            uname = str(session_data.get("username") or "").strip().lower()
            if not uname:
                return None
            if self._users is not None:
                user_doc = await self._users.find_by_username(uname)
                if not user_doc:
                    return None
                expected_ver = str(user_doc.get("session_version") or "")
                cookie_ver = str(session_data.get("session_version") or "")
                if expected_ver and (not cookie_ver or not hmac.compare_digest(cookie_ver, expected_ver)):
                    return None
                role = str(user_doc.get("role") or "operator")
                session_data["role"] = role
                session_data["is_admin"] = role == "admin"
            return self.public_user_payload(session_data)

        return None

    async def require_user(self, request: Request) -> dict[str, Any]:
        """Dependency bắt buộc người dùng đã đăng nhập hợp lệ (401 nếu chưa đăng nhập)."""
        user = await self.get_current_user(request)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Chưa đăng nhập hoặc phiên đã hết hạn.",
            )
        return user

    def make_require_admin(self):
        """Tạo dependency bắt buộc quyền Admin (403 nếu tài khoản chỉ có quyền Operator)."""
        async def _require_admin(user: dict[str, Any] = Depends(self.require_user)) -> dict[str, Any]:
            if not user.get("is_admin"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Chỉ tài khoản Admin mới có quyền thực hiện thao tác này.",
                )
            return user

        return _require_admin

    @property
    def require_admin(self):
        """Thuộc tính trả về dependency kiểm tra quyền Admin dùng trong `Depends(...)`."""
        if not hasattr(self, "_require_admin_dep"):
            self._require_admin_dep = self.make_require_admin()
        return self._require_admin_dep
