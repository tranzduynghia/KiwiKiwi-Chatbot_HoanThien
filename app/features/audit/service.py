"""Dịch vụ nghiệp vụ ghi nhận và truy vấn Audit Log đã lọc trường nhạy cảm."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.diff import is_sensitive_field, mask_secret_preview, sanitize_audit_value
from app.features.audit.repository import AuditRepository


class AuditService:
    """Chuẩn hóa bản ghi kiểm toán, đưa vào bộ đệm RAM và kích hoạt đồng bộ lên Atlas."""

    def __init__(self, repository: AuditRepository, sync_coordinator: WriteBehindSyncCoordinator):
        self._repository = repository
        self._sync = sync_coordinator

    async def record(
        self,
        *,
        actor: str,
        actor_type: str = "dashboard",
        action: str,
        section: str,
        summary: str,
        changes: Optional[list[dict[str, Any]]] = None,
        timestamp: Optional[str] = None,
        actor_meta: Optional[dict[str, Any]] = None,
        source: Optional[str] = None,
        extra_fields: Optional[dict[str, Any]] = None,
        flush: bool = True,
    ) -> dict[str, Any]:
        """Ghi một bản ghi Audit Log sau khi loại bỏ/che trường nhạy cảm và kèm thông tin avatar/nguồn thao tác."""
        safe_changes = []
        for change in changes or []:
            field = str(change.get("field", ""))
            if is_sensitive_field(field):
                if change.get("masked"):
                    safe_changes.append({
                        "field": field,
                        "before": mask_secret_preview(change.get("before")),
                        "after": mask_secret_preview(change.get("after")),
                    })
                continue
            safe_changes.append({
                "field": field,
                "before": sanitize_audit_value(change.get("before")),
                "after": sanitize_audit_value(change.get("after")),
            })
        meta = actor_meta or {}
        iso_now = timestamp or datetime.now(timezone.utc).isoformat()
        doc: dict[str, Any] = {
            "at": iso_now,
            "timestamp": iso_now,
            "actor": actor,
            "actor_type": actor_type,
            "actor_display_name": str(meta.get("actor_display_name") or actor),
            "actor_discord_id": str(meta.get("actor_discord_id") or ""),
            "actor_avatar_url": str(meta.get("actor_avatar_url") or ""),
            "action": action,
            "section": section,
            "summary": summary,
            "changes": safe_changes,
        }
        if source:
            doc["source"] = str(source)
        if isinstance(extra_fields, dict):
            for key, val in extra_fields.items():
                key_str = str(key or "").strip()
                if not key_str or key_str in doc or is_sensitive_field(key_str):
                    continue
                doc[key_str] = sanitize_audit_value(val)
        self._repository.enqueue(doc)
        if flush:
            await self._sync.flush_pending()
        return doc

    async def list_logs(self, limit: int = 100, section: Optional[str] = None) -> list[dict[str, Any]]:
        """Trả về danh sách nhật ký chỉnh sửa mới nhất theo chuyên mục (nếu có)."""
        return await self._repository.list_logs(limit=limit, section=section)

    async def list_logs_paginated(
        self,
        *,
        page: int = 1,
        limit: int = 5,
        section: Optional[str] = None,
    ) -> dict[str, Any]:
        """Truy vấn phân trang Audit Log phía server (mặc định 5 mục/trang, tối đa 20 mục)."""
        return await self._repository.list_logs_paginated(page=page, limit=limit, section=section)
