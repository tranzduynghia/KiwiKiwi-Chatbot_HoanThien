"""Router FastAPI quản lý phân quyền Discord User ID trên Web Dashboard."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.sync import WriteBehindSyncCoordinator
from app.features.permissions.schemas import DiscordPermissionPayload
from app.features.permissions.service import PermissionService


def create_permissions_router(
    permission_service: PermissionService,
    sync_coordinator: WriteBehindSyncCoordinator,
    auth_deps: Any,
) -> APIRouter:
    """Tạo router cho nhóm endpoint `/api/permissions/discord`."""
    router = APIRouter(tags=["permissions"])

    @router.get("/api/permissions/discord")
    async def api_list_discord_perms(user: dict[str, Any] = Depends(auth_deps.require_user)):
        return {
            "items": await permission_service.list_permissions(),
            "can_manage": bool(user.get("is_admin")),
        }

    @router.post("/api/permissions/discord")
    async def api_upsert_discord_perm(
        payload: DiscordPermissionPayload,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        try:
            actor, actor_type = auth_deps.actor_label(admin)
            doc = await permission_service.upsert_permission(
                payload.user_id,
                label=payload.label,
                is_admin=payload.is_admin,
                can_dashboard=payload.can_dashboard,
                can_status=payload.can_status,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
            return {"ok": True, "item": doc, "sync": sync_coordinator.sync_status()}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/permissions/discord/{user_id}")
    async def api_delete_discord_perm(
        user_id: str,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        if admin.get("auth_type") == "discord" and str(admin.get("discord_id")) == str(user_id):
            raise HTTPException(
                status_code=400,
                detail="Không thể tự xóa quyền của chính bạn đang đăng nhập.",
            )
        actor, actor_type = auth_deps.actor_label(admin)
        deleted = await permission_service.delete_permission(
            user_id,
            actor=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(admin),
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Không tìm thấy Discord User ID này.")
        return {"ok": True}

    return router
