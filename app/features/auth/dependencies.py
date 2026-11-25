"""Dependency xác thực phiên đăng nhập FastAPI và tiện ích cookie cho Dashboard."""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.security import SessionTokenSigner
from app.features.permissions.service import PermissionService

SESSION_COOKIE_NAME = "kiwikiwi_session"
OAUTH_STATE_COOKIE_NAME = "kiwikiwi_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 ngày
OAUTH_STATE_MAX_AGE = 300  # 5 phút


class AuthDependencies:
    """Cung cấp các dependency FastAPI (`require_user`, `require_admin`) và xử lý cookie phiên."""

    def __init__(self, signer: SessionTokenSigner, permission_service: PermissionService):
        self.signer = signer
        self._permissions = permission_service

    @staticmethod
    def is_secure_request(request: Request) -> bool:
        """Xác định request đang chạy qua HTTPS (hoặc reverse proxy HTTPS) để gắn cờ Secure cho cookie."""
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        public_url = (os.getenv("DASHBOARD_PUBLIC_URL") or "").lower()
        return proto == "https" or public_url.startswith("https://")

    @staticmethod
    def actor_label(user: dict[str, Any]) -> tuple[str, str]:
        """Tạo nhãn người thao tác và loại xác thực phục vụ Audit Log."""
        if user.get("auth_type") == "discord":
            display = user.get("display_name") or user.get("username")
            return f"{display} (discord:{user.get('discord_id')})", "discord"
        return str(user.get("username") or "dashboard"), "password"

    def set_session_cookie(self, response: Response, request: Request, session_data: dict[str, Any]) -> None:
        """Gắn cookie phiên đã ký bảo mật vào HTTP response."""
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=self.signer.encode(session_data),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=self.is_secure_request(request),
        )

    def clear_session_cookie(self, response: Response) -> None:
        """Xóa cookie phiên đăng nhập khỏi trình duyệt."""
        response.delete_cookie(SESSION_COOKIE_NAME)

    async def get_current_user(self, request: Request) -> Optional[dict[str, Any]]:
        """Giải mã phiên hiện tại; với tài khoản Discord OAuth2 sẽ kiểm tra lại quyền trực tiếp từ MongoDB."""
        session_data = self.signer.decode(request.cookies.get(SESSION_COOKIE_NAME), max_age=SESSION_MAX_AGE)
        if not session_data:
            return None
        if session_data.get("auth_type") == "discord":
            uid = session_data.get("discord_id")
            if not uid:
                return None
            allowed, is_admin = await self._permissions.can_access_dashboard(uid)
            if not allowed:
                return None
            session_data["is_admin"] = is_admin
            return session_data
        if session_data.get("auth_type") == "password" and session_data.get("username"):
            return session_data
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
