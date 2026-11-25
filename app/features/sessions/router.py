"""Router FastAPI phục vụ tra cứu nội dung phiên chat trực tiếp từ MongoDB Atlas."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.features.sessions.repository import SessionRepository


def create_sessions_router(session_repo: SessionRepository, auth_deps: Any) -> APIRouter:
    """Tạo router cho nhóm API `/api/sessions/search`."""
    router = APIRouter(tags=["sessions"])

    @router.get("/api/sessions/search")
    async def api_search_sessions(
        q: str = Query(default="", max_length=200),
        user_id: str = Query(default="", max_length=40),
        role: Optional[str] = Query(default=None),
        limit: int = Query(default=100, ge=1, le=300),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        items = await session_repo.search_messages(
            query=q,
            user_id=user_id,
            role=role or "",
            limit=limit,
        )
        return {
            "items": items,
            "total": len(items),
            "db_connected": session_repo.archives_col is not None and session_repo.ok,
        }

    return router
