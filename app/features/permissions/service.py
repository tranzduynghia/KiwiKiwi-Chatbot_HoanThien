"""Dịch vụ nghiệp vụ kiểm tra và quản lý phân quyền theo Discord User ID."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.diff import compute_diff
from app.features.audit.service import AuditService
from app.features.permissions.repository import PermissionRepository


class PermissionService:
    """Xử lý kiểm tra quyền `/status`, quyền truy cập Dashboard qua Discord OAuth2 và CRUD phân quyền."""

    def __init__(
        self,
        repository: PermissionRepository,
        audit_service: AuditService,
        sync_coordinator: WriteBehindSyncCoordinator,
    ):
        self._repository = repository
        self._audit = audit_service
        self._sync = sync_coordinator

    async def list_permissions(self) -> list[dict[str, Any]]:
        """Lấy toàn bộ danh sách phân quyền Discord, sắp xếp Admin lên đầu."""
        perms = await self._repository.refresh(force=True)
        items = list(perms.values())
        items.sort(key=lambda item: (not item.get("is_admin", False), item.get("user_id", "")))
        return items

    async def get_permission(self, user_id: int | str, *, force: bool = True) -> Optional[dict[str, Any]]:
        """Tra cứu bản ghi phân quyền của một Discord User ID."""
        uid = str(user_id).strip()
        perms = await self._repository.refresh(force=force)
        return perms.get(uid)

    async def upsert_permission(
        self,
        user_id: str,
        *,
        label: str = "",
        is_admin: bool = False,
        can_dashboard: bool = False,
        can_status: bool = False,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Thêm hoặc cập nhật quyền cho một Discord User ID kèm ghi Audit Log và đồng bộ Atlas."""
        uid = str(user_id).strip()
        if not uid.isdigit():
            raise ValueError("Discord User ID phải là chuỗi chữ số hợp lệ.")
        if is_admin:
            can_dashboard = True
            can_status = True

        before = self._repository.get_cached(uid)
        now_iso = datetime.now(timezone.utc).isoformat()
        doc = {
            "_id": uid,
            "user_id": uid,
            "label": label.strip()[:80],
            "is_admin": bool(is_admin),
            "can_dashboard": bool(can_dashboard),
            "can_status": bool(can_status),
            "updated_at": now_iso,
        }
        self._repository.stage_upsert(doc)

        if actor:
            changes = compute_diff(before or {}, doc, prefix=f"discord:{uid}")
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="discord_perm_upsert",
                section="discord_permissions",
                summary=f"{'Cập nhật' if before else 'Thêm'} quyền cho Discord ID {uid}",
                changes=changes,
                timestamp=now_iso,
                flush=False,
            )

        await self._sync.flush_pending()
        return doc

    async def delete_permission(
        self,
        user_id: str,
        *,
        actor: Optional[str] = None,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Xóa phân quyền của một Discord User ID khỏi RAM và MongoDB Atlas."""
        uid = str(user_id).strip()
        before = self._repository.get_cached(uid)
        if before is None:
            return False
        self._repository.stage_delete(uid)

        if actor:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="discord_perm_delete",
                section="discord_permissions",
                summary=f"Xóa phân quyền Discord ID {uid}",
                changes=[{"field": f"discord:{uid}", "before": before, "after": None}],
                flush=False,
            )

        await self._sync.flush_pending()
        return True

    async def can_use_status(self, user_id: int | str) -> bool:
        """Kiểm tra Discord User ID có được phép thực thi lệnh `/status` hay không."""
        doc = await self.get_permission(user_id, force=True)
        if not doc:
            return False
        return bool(doc.get("is_admin") or doc.get("can_status"))

    async def can_access_dashboard(self, user_id: int | str) -> tuple[bool, bool]:
        """Trả về cặp `(allowed, is_admin)` cho đăng nhập Discord OAuth2 trên Dashboard."""
        doc = await self.get_permission(user_id, force=True)
        if not doc:
            return False, False
        is_admin = bool(doc.get("is_admin"))
        return bool(is_admin or doc.get("can_dashboard")), is_admin

    async def has_any_discord_admin(self) -> bool:
        """Kiểm tra hệ thống có còn ít nhất một Discord Admin nào hay không."""
        perms = await self.list_permissions()
        return any(item.get("is_admin") for item in perms)
