"""Router FastAPI phục vụ tra cứu, lọc theo vai trò server/UID/username và duyệt toàn bộ phiên chat."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.features.sessions.discord_lookup import DiscordUserLookupService
from app.features.sessions.repository import SessionRepository


def create_sessions_router(
    session_repo: SessionRepository,
    get_bot: Callable[[], Any],
    auth_deps: Any,
    user_lookup: Optional[DiscordUserLookupService] = None,
) -> APIRouter:
    """Tạo router cho `/api/sessions/filters`, `/api/sessions/search` và `/api/sessions/{chat_id}`."""
    router = APIRouter(tags=["sessions"])
    lookup = user_lookup or DiscordUserLookupService()

    @router.get("/api/sessions/filters")
    async def api_session_filters(_user: dict[str, Any] = Depends(auth_deps.require_user)):
        """Trả về danh sách vai trò server (từ bot Discord đang chạy gộp với MongoDB) cho bộ lọc."""
        bot = get_bot()
        merged_roles: dict[str, dict[str, str]] = {}
        for role in lookup.list_server_roles(bot):
            merged_roles[role["id"]] = role
        for role in await session_repo.list_stored_server_roles():
            merged_roles.setdefault(role["id"], role)

        roles_list = sorted(
            merged_roles.values(),
            key=lambda item: (item.get("guild_name", "").lower(), item.get("name", "").lower()),
        )
        return {"server_roles": roles_list}

    @router.get("/api/sessions/search")
    async def api_search_sessions(
        q: str = Query(default="", max_length=200),
        user_id: str = Query(default="", max_length=40),
        username: str = Query(default="", max_length=64),
        server_role: str = Query(default="", max_length=80),
        role: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1000),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        bot = get_bot()

        async def _resolve_user(uid: str) -> dict[str, Any]:
            return await lookup.resolve_user_profile(bot, uid)

        result = await session_repo.search_messages(
            query=q,
            user_id=user_id,
            username=username,
            server_role=server_role,
            role=role or "",
            limit=limit,
            offset=offset,
            user_resolver=_resolve_user,
        )
        return {
            "items": result["items"],
            "total": result["total"],
            "has_more": result["has_more"],
            "offset": offset,
            "limit": limit,
            "db_connected": session_repo.archives_col is not None and session_repo.ok,
        }

    @router.get("/api/sessions/{chat_id}")
    async def api_session_detail(
        chat_id: str,
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Trả về toàn bộ lịch sử tin nhắn và siêu dữ liệu của một phiên chat cụ thể."""
        bot = get_bot()

        async def _resolve_user(uid: str) -> dict[str, Any]:
            return await lookup.resolve_user_profile(bot, uid)

        detail = await session_repo.get_session_detail(chat_id, user_resolver=_resolve_user)
        if detail is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy phiên chat này trong MongoDB Atlas.")
        return {"session": detail}

    return router
