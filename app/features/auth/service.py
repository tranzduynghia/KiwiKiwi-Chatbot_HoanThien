"""Dịch vụ nghiệp vụ xác thực mật khẩu và quản trị tài khoản Dashboard."""
from __future__ import annotations

import copy
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.errors import AlreadyExistsError
from app.core.security import hash_password_async, verify_password_with_migration_async
from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.service import AuditService
from app.features.auth.repository import UserRepository
from app.features.permissions.service import PermissionService

log = logging.getLogger("gemini-bot.auth")


class AuthService:
    """Xử lý đăng nhập mật khẩu và CRUD tài khoản `dashboard_users` có ghi Audit Log."""

    def __init__(
        self,
        user_repository: UserRepository,
        permission_service: PermissionService,
        audit_service: AuditService,
        sync_coordinator: WriteBehindSyncCoordinator,
    ):
        self._users = user_repository
        self._permissions = permission_service
        self._audit = audit_service
        self._sync = sync_coordinator

    async def list_users(self) -> list[dict[str, Any]]:
        """Trả về danh sách tài khoản Dashboard đã lược bỏ hoàn toàn `password_hash`, `salt` và `session_version`."""
        users_map = await self._users.refresh_all()
        output = []
        for uname in sorted(users_map.keys()):
            doc = users_map[uname]
            output.append({
                "username": doc.get("username", ""),
                "role": doc.get("role", "admin"),
                "created_at": doc.get("created_at"),
                "last_login_at": doc.get("last_login_at"),
            })
        return output

    async def create_user(
        self,
        username: str,
        password: str,
        role: str = "admin",
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Tạo tài khoản Dashboard mới với mật khẩu băm scrypt."""
        uname = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{3,32}", uname):
            raise ValueError("Tên đăng nhập phải dài 3–32 ký tự (chữ thường, số, _, ., -).")
        if len(password) < 6:
            raise ValueError("Mật khẩu phải có ít nhất 6 ký tự.")
        if role not in ("admin", "operator"):
            raise ValueError("Vai trò không hợp lệ (chỉ nhận admin hoặc operator).")

        users_map = await self._users.refresh_all()
        if uname in users_map:
            raise AlreadyExistsError(f"Tài khoản '{uname}' đã tồn tại.")

        now_iso = datetime.now(timezone.utc).isoformat()
        doc = {
            "username": uname,
            "password_hash": await hash_password_async(password),
            "session_version": secrets.token_hex(8),
            "role": role,
            "created_at": now_iso,
            "last_login_at": None,
        }
        self._users.stage_upsert(doc)

        if actor:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="user_create",
                section="dashboard_users",
                summary=f"Tạo tài khoản Dashboard '{uname}' (vai trò: {role})",
                changes=[
                    {"field": "username", "before": None, "after": uname},
                    {"field": "role", "before": None, "after": role},
                ],
                timestamp=now_iso,
                flush=False,
            )

        await self._sync.flush_pending()
        return {"username": uname, "role": role, "created_at": now_iso, "last_login_at": None}

    async def change_password(
        self,
        username: str,
        new_password: str,
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Đổi mật khẩu tài khoản hiện có (băm scrypt + xoay `session_version` thu hồi ngay phiên cũ)."""
        uname = username.strip().lower()
        if len(new_password) < 6:
            raise ValueError("Mật khẩu mới phải có ít nhất 6 ký tự.")

        users_map = await self._users.refresh_all()
        if uname not in users_map:
            return False

        doc = copy.deepcopy(users_map[uname])
        doc["password_hash"] = await hash_password_async(new_password)
        doc["session_version"] = secrets.token_hex(8)
        self._users.stage_upsert(doc)

        if actor:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="user_password_change",
                section="dashboard_users",
                summary=f"Đổi mật khẩu tài khoản '{uname}' (mật khẩu được ẩn)",
                changes=[{"field": "account", "before": uname, "after": "password_updated"}],
                flush=False,
            )

        await self._sync.flush_pending()
        return True

    async def delete_user(
        self,
        username: str,
        *,
        current_admin: Optional[dict[str, Any]] = None,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Xóa tài khoản Dashboard, chặn tự xóa chính mình hoặc xóa Admin cuối cùng của hệ thống."""
        uname = username.strip().lower()
        if current_admin and current_admin.get("auth_type") == "password" and current_admin.get("username") == uname:
            raise ValueError("Không thể tự xóa tài khoản đang đăng nhập.")

        users = await self.list_users()
        if not any(item["username"] == uname for item in users):
            return False

        admins = [item for item in users if item.get("role") == "admin"]
        if len(admins) <= 1 and any(item["username"] == uname and item.get("role") == "admin" for item in users):
            if not await self._permissions.has_any_discord_admin():
                raise ValueError("Không thể xóa tài khoản Admin cuối cùng.")

        self._users.stage_delete(uname)

        if actor:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="user_delete",
                section="dashboard_users",
                summary=f"Xóa tài khoản Dashboard '{uname}'",
                changes=[{"field": "username", "before": uname, "after": None}],
                flush=False,
            )

        await self._sync.flush_pending()
        return True

    async def authenticate_password(self, username: str, password: str) -> Optional[dict[str, Any]]:
        """Xác thực mật khẩu (hỗ trợ HMAC-SHA256 Pepper + tự động nâng cấp mượt hash cũ sang bản có pepper khi đăng nhập)."""
        uname = username.strip().lower()
        doc = await self._users.find_by_username(uname)
        if not doc:
            return None

        is_valid, needs_migration = await verify_password_with_migration_async(
            password,
            doc.get("password_hash", ""),
        )
        if not is_valid:
            return None

        if needs_migration:
            upgraded_hash = await hash_password_async(password)
            await self._users.upgrade_password_hash(uname, upgraded_hash)
            log.info("Đã tự động nâng cấp mật khẩu của tài khoản '%s' sang chuẩn HMAC-SHA256 Pepper.", uname)

        now_iso = datetime.now(timezone.utc).isoformat()
        await self._users.record_last_login(uname, now_iso)
        role = doc.get("role", "admin")
        return {
            "username": uname,
            "role": role,
            "is_admin": role == "admin",
            "session_version": str(doc.get("session_version") or ""),
        }

    async def verify_user_password(self, username: str, password: str) -> bool:
        """Kiểm tra mật khẩu hiện tại của tài khoản Dashboard (hỗ trợ Pepper) phục vụ Step-up Authentication."""
        uname = str(username or "").strip().lower()
        if not uname or not password:
            return False
        doc = await self._users.find_by_username(uname)
        if not doc:
            return False
        is_valid, _needs_migration = await verify_password_with_migration_async(
            password,
            str(doc.get("password_hash") or ""),
        )
        return bool(is_valid)
