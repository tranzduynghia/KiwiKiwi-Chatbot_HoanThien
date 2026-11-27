"""Dependency xác thực phiên đăng nhập FastAPI và tiện ích cookie cho Dashboard."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Optional

from fastapi import Depends, HTTPException, Request, Response, status

from app.core.security import SessionTokenSigner, is_trusted_proxy_ip, sanitize_https_url
from app.features.permissions.service import PermissionService

SESSION_COOKIE_NAME = "kiwikiwi_session"
OAUTH_STATE_COOKIE_NAME = "kiwikiwi_oauth_state"
STEP_UP_COOKIE_NAME = "kiwikiwi_stepup"
STEP_UP_IDLE_SECONDS = 60
STEP_UP_DETAIL = "STEP_UP_REQUIRED"
DEFAULT_SESSION_MAX_AGE = 60 * 60 * 24  # 24 giờ (mặc định an toàn)
OAUTH_STATE_MAX_AGE = 300  # 5 phút


class StepUpRequiredError(Exception):
    """Ngoại lệ bắt buộc xác thực lại (Step-up Auth) — trả HTTP 403 và xóa cookie kiwikiwi_stepup."""


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
        config_repo: Any = None,
    ):
        self.signer = signer
        self.stepup_signer = signer.derive_signer("kiwikiwi-stepup-v1")
        self._permissions = permission_service
        self._users = user_repo
        self._config_repo = config_repo

    def set_config_repo(self, config_repo: Any) -> None:
        """Gắn `ConfigRepository` để đọc động `locked_sections` và `step_up_cooldown_sec`."""
        self._config_repo = config_repo

    def get_step_up_cooldown_seconds(self) -> int:
        """Đọc thời gian cooldown Step-up (15..300 giây, mặc định 60s) từ cấu hình động."""
        if self._config_repo is not None:
            cfg = getattr(self._config_repo, "current_config", None) or {}
            sec = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
            raw = sec.get("step_up_cooldown_sec", STEP_UP_IDLE_SECONDS)
            try:
                return max(15, min(300, int(raw)))
            except (TypeError, ValueError):
                pass
        return STEP_UP_IDLE_SECONDS

    def get_locked_sections(self) -> list[str]:
        """Trả về danh sách các mục đang bật khóa Step-up Auth."""
        default_locked = ["perms", "sessions", "temp"]
        if self._config_repo is not None:
            cfg = getattr(self._config_repo, "current_config", None) or {}
            sec = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
            raw_list = sec.get("locked_sections")
            if isinstance(raw_list, list):
                return [str(item).strip().lower() for item in raw_list if str(item).strip()]
        return default_locked

    def is_section_locked(self, section: str) -> bool:
        """Kiểm tra mục có đang bật khóa Step-up hay không (`dashboard_settings` luôn khóa cứng)."""
        key = str(section or "").strip().lower()
        if key in ("dashboard_settings", "security"):
            return True
        return key in self.get_locked_sections()

    @staticmethod
    def resolve_user_id(session_data: dict[str, Any]) -> str:
        """Tạo định danh duy nhất gắn với tài khoản đăng nhập hiện tại."""
        auth_type = str(session_data.get("auth_type") or "").strip()
        if auth_type == "discord":
            uid = str(session_data.get("discord_id") or "").strip()
            return f"discord:{uid}" if uid else ""
        if auth_type == "password":
            uname = str(session_data.get("username") or "").strip().lower()
            return f"password:{uname}" if uname else ""
        return ""

    @classmethod
    def resolve_session_id(cls, session_data: dict[str, Any]) -> str:
        """Trích xuất session_id của phiên hiện tại (fallback ổn định nếu cookie cũ chưa có sid)."""
        sid = str(session_data.get("session_id") or "").strip()
        if sid:
            return sid
        fallback_seed = "|".join([
            cls.resolve_user_id(session_data),
            str(session_data.get("login_at") or ""),
            str(session_data.get("session_version") or ""),
        ])
        return hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()[:24]

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

    def set_step_up_cookie(
        self,
        response: Response,
        request: Request,
        *,
        user_id: str,
        session_id: str,
        method: str = "password",
        now: Optional[int] = None,
    ) -> int:
        """Cấp/trượt cookie Step-up Auth không trạng thái (`kiwikiwi_stepup`) gắn chặt user_id + session_id (60s)."""
        ts = int(time.time()) if now is None else int(now)
        payload = {
            "uid": str(user_id),
            "sid": str(session_id),
            "last_active_at": ts,
            "method": str(method or "password"),
        }
        cooldown = self.get_step_up_cooldown_seconds()
        response.set_cookie(
            key=STEP_UP_COOKIE_NAME,
            value=self.stepup_signer.encode(payload),
            max_age=cooldown,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self.is_secure_request(request),
        )
        return ts

    def clear_step_up_cookie(self, response: Response, request: Optional[Request] = None) -> None:
        """Xóa ngay cookie Step-up Auth khỏi trình duyệt."""
        response.delete_cookie(
            key=STEP_UP_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=self.is_secure_request(request) if request is not None else False,
        )

    def inspect_step_up(
        self,
        request: Request,
        *,
        user_id: str,
        session_id: str,
    ) -> tuple[bool, int, Optional[dict[str, Any]]]:
        """Kiểm tra cookie `kiwikiwi_stepup` còn hạn nhàn rỗi <= 60s và khớp đúng user_id + session_id hay không."""
        if not user_id or not session_id:
            return False, 0, None
        raw_cookie = request.cookies.get(STEP_UP_COOKIE_NAME)
        if not raw_cookie:
            return False, 0, None
        payload = self.stepup_signer.decode(raw_cookie, max_age=SESSION_MAX_AGE)
        if not isinstance(payload, dict):
            return False, 0, None
        token_uid = str(payload.get("uid") or "")
        token_sid = str(payload.get("sid") or "")
        if not token_uid or not token_sid:
            return False, 0, None
        if not (hmac.compare_digest(token_uid, user_id) and hmac.compare_digest(token_sid, session_id)):
            return False, 0, None
        raw_ts = payload.get("last_active_at")
        if not isinstance(raw_ts, (int, float)):
            return False, 0, None
        cooldown = float(self.get_step_up_cooldown_seconds())
        now = time.time()
        elapsed = now - float(raw_ts)
        if elapsed < -5.0 or elapsed > cooldown:
            return False, 0, None
        remaining = max(0, int(cooldown - max(0.0, elapsed)))
        return True, remaining, payload

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
            request.state.auth_user_id = self.resolve_user_id(session_data)
            request.state.auth_session_id = self.resolve_session_id(session_data)
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
            request.state.auth_user_id = self.resolve_user_id(session_data)
            request.state.auth_session_id = self.resolve_session_id(session_data)
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

    def make_require_step_up_auth(self):
        """Tạo dependency bắt buộc Step-up Auth (<= 60s nhàn rỗi) và trượt `last_active_at` chỉ trên endpoint nhạy cảm."""
        async def _require_step_up_auth(
            request: Request,
            response: Response,
            user: dict[str, Any] = Depends(self.require_user),
        ) -> dict[str, Any]:
            user_id = str(getattr(request.state, "auth_user_id", "") or "")
            session_id = str(getattr(request.state, "auth_session_id", "") or "")
            valid, _remaining, payload = self.inspect_step_up(
                request,
                user_id=user_id,
                session_id=session_id,
            )
            if not valid or payload is None:
                raise StepUpRequiredError()
            self.set_step_up_cookie(
                response,
                request,
                user_id=user_id,
                session_id=session_id,
                method=str(payload.get("method") or "password"),
            )
            return user

        return _require_step_up_auth

    @property
    def require_step_up_auth(self):
        """Thuộc tính trả về dependency bắt buộc Step-up Auth cho mọi thao tác đọc/ghi nhạy cảm."""
        if not hasattr(self, "_require_step_up_dep"):
            self._require_step_up_dep = self.make_require_step_up_auth()
        return self._require_step_up_dep

    def make_require_admin_step_up(self):
        """Tạo dependency kết hợp quyền Admin + Step-up Auth cho mọi thao tác ghi/xóa mục Phân quyền & Admin."""
        async def _require_admin_step_up(
            user: dict[str, Any] = Depends(self.require_admin),
            _step_up: dict[str, Any] = Depends(self.require_step_up_auth),
        ) -> dict[str, Any]:
            return user

        return _require_admin_step_up

    @property
    def require_admin_step_up(self):
        """Thuộc tính trả về dependency bắt buộc Admin + Step-up Auth."""
        if not hasattr(self, "_require_admin_step_up_dep"):
            self._require_admin_step_up_dep = self.make_require_admin_step_up()
        return self._require_admin_step_up_dep

    def require_section_unlocked(self, section: str):
        """Dependency kiểm tra Step-up động theo `locked_sections` cho từng mục trên Dashboard."""
        async def _dep(
            request: Request,
            response: Response,
            user: dict[str, Any] = Depends(self.require_user),
        ) -> dict[str, Any]:
            if not self.is_section_locked(section):
                return user
            user_id = str(getattr(request.state, "auth_user_id", "") or "")
            session_id = str(getattr(request.state, "auth_session_id", "") or "")
            valid, _remaining, payload = self.inspect_step_up(
                request,
                user_id=user_id,
                session_id=session_id,
            )
            if not valid or payload is None:
                raise StepUpRequiredError()
            self.set_step_up_cookie(
                response,
                request,
                user_id=user_id,
                session_id=session_id,
                method=str(payload.get("method") or "password"),
            )
            return user

        return _dep

    def require_admin_section_unlocked(self, section: str):
        """Dependency bắt buộc Admin + kiểm tra Step-up động theo `locked_sections`."""
        section_dep = self.require_section_unlocked(section)

        async def _dep(
            admin: dict[str, Any] = Depends(self.require_admin),
            _unlocked: dict[str, Any] = Depends(section_dep),
        ) -> dict[str, Any]:
            return admin

        return _dep
