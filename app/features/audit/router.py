"""Router FastAPI phục vụ truy vấn nhật ký chỉnh sửa Audit Log."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.features.audit.service import AuditService


def create_audit_router(audit_service: AuditService, auth_deps: Any) -> APIRouter:
    """Tạo router cho nhóm API `/api/audit-logs`."""
    router = APIRouter(tags=["audit"])

    @router.get("/api/audit-logs")
    async def api_audit_logs(
        limit: int = Query(default=100, ge=1, le=300),
        section: Optional[str] = Query(default=None),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        return {"items": await audit_service.list_logs(limit=limit, section=section)}

    return router
