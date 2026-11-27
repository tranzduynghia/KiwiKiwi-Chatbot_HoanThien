"""Router FastAPI quản lý phân quyền Discord User ID trên Web Dashboard."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.sync import WriteBehindSyncCoordinator
from app.core.timezone import format_timezone_badge, resolve_request_timezone
from app.features.permissions.blacklist import BlacklistEntryPayload
from app.features.permissions.schemas import DiscordPermissionPayload
from app.features.permissions.service import PermissionService


def _validate_csrf_origin(request: Request) -> None:
    """Xác thực header `Origin` khớp với `Host` để chống giả mạo yêu cầu xuyên nguồn (CSRF)."""
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    parsed = urlparse(origin)
    origin_host = (parsed.netloc or "").strip().lower()
    req_host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
    if origin_host and req_host and origin_host != req_host:
        raise HTTPException(status_code=403, detail="Origin không hợp lệ (bảo vệ CSRF).")


def create_permissions_router(
    permission_service: PermissionService,
    sync_coordinator: WriteBehindSyncCoordinator,
    auth_deps: Any,
    blacklist_service: Any = None,
) -> APIRouter:
    """Tạo router cho nhóm endpoint `/api/permissions/discord` và `/api/permissions/blacklist`."""
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

    @router.get("/api/permissions/blacklist")
    async def api_list_blacklist(
        request: Request,
        limit: int = Query(default=15, ge=1, le=50),
        cursor: Optional[str] = Query(default=None),
        q: str = Query(default=""),
        tz: Optional[str] = Query(default=None),
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        tz_name, tz_info = resolve_request_timezone(request.headers, fallback_tz=tz)
        if blacklist_service is None:
            return {
                "items": [],
                "total": 0,
                "limit": limit,
                "cursor": "0",
                "next_cursor": None,
                "prev_cursor": None,
                "has_more": False,
                "timezone": tz_name,
                "timezone_label": format_timezone_badge(tz_name, tz_info),
                "can_manage": bool(user.get("is_admin")),
            }
        result = await blacklist_service.list_entries(
            limit=limit,
            cursor=cursor,
            search=q,
            tz_info=tz_info,
        )
        return {
            **result,
            "timezone": tz_name,
            "timezone_label": format_timezone_badge(tz_name, tz_info),
            "can_manage": bool(user.get("is_admin")),
        }

    @router.post("/api/permissions/blacklist")
    async def api_upsert_blacklist(
        payload: BlacklistEntryPayload,
        request: Request,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        _validate_csrf_origin(request)
        if blacklist_service is None:
            raise HTTPException(status_code=503, detail="Dịch vụ danh sách đen chưa sẵn sàng.")
        actor, actor_type = auth_deps.actor_label(admin)
        try:
            doc = await blacklist_service.ban_user(
                discord_user_id=payload.discord_user_id,
                reason=payload.reason,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
            return {
                "ok": True,
                "item": {
                    "discord_user_id": doc["discord_user_id"],
                    "reason": doc["reason"],
                    "banned_by": doc["banned_by"],
                },
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/permissions/blacklist/{discord_user_id}")
    async def api_delete_blacklist(
        discord_user_id: str,
        request: Request,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        _validate_csrf_origin(request)
        if blacklist_service is None:
            raise HTTPException(status_code=503, detail="Dịch vụ danh sách đen chưa sẵn sàng.")
        actor, actor_type = auth_deps.actor_label(admin)
        try:
            removed = await blacklist_service.unban_user(
                discord_user_id=discord_user_id,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Discord User ID này trong danh sách đen.")
        return {"ok": True, "discord_user_id": discord_user_id}

    return router
