"""Dịch vụ nghiệp vụ ghi nhận và truy vấn Audit Log đã lọc trường nhạy cảm."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.core.sync import WriteBehindSyncCoordinator
from app.features.audit.diff import is_sensitive_field, sanitize_audit_value
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
        flush: bool = True,
    ) -> dict[str, Any]:
        """Ghi một bản ghi Audit Log sau khi loại bỏ trường nhạy cảm và cắt gọn giá trị dài."""
        safe_changes = []
        for change in changes or []:
            field = str(change.get("field", ""))
            if is_sensitive_field(field):
                continue
            safe_changes.append({
                "field": field,
                "before": sanitize_audit_value(change.get("before")),
                "after": sanitize_audit_value(change.get("after")),
            })
        doc = {
            "at": timestamp or datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "actor_type": actor_type,
            "action": action,
            "section": section,
            "summary": summary,
            "changes": safe_changes,
        }
        self._repository.enqueue(doc)
        if flush:
            await self._sync.flush_pending()
        return doc

    async def list_logs(self, limit: int = 100, section: Optional[str] = None) -> list[dict[str, Any]]:
        """Trả về danh sách nhật ký chỉnh sửa mới nhất theo chuyên mục (nếu có)."""
        return await self._repository.list_logs(limit=limit, section=section)
