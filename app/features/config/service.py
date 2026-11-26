"""Dịch vụ cập nhật cấu hình động kèm tính toán diff, ghi Audit Log và thông báo callback."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.sync import WriteBehindSyncCoordinator
from app.core.utils import deep_merge
from app.features.audit.diff import compute_diff, merge_client_changes
from app.features.audit.service import AuditService
from app.features.config.repository import ConfigRepository

log = logging.getLogger("gemini-bot.config")


class ConfigService:
    """Điều phối đọc/cập nhật cấu hình động theo thời gian thực không cần restart bot."""

    def __init__(
        self,
        repository: ConfigRepository,
        audit_service: AuditService,
        sync_coordinator: WriteBehindSyncCoordinator,
    ):
        self._repository = repository
        self._audit = audit_service
        self._sync = sync_coordinator
        self._callbacks: list[Callable[[dict[str, Any]], None]] = []

    def on_update(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Đăng ký hàm callback chạy mỗi khi cấu hình toàn cục được cập nhật."""
        self._callbacks.append(callback)

    async def get_config(self, force: bool = False) -> dict[str, Any]:
        """Lấy bản sao cấu hình hiện hành từ kho cấu hình."""
        return await self._repository.get_config(force=force)

    async def update_config(
        self,
        patch: dict[str, Any],
        updated_by: str = "dashboard",
        *,
        actor_type: str = "dashboard",
        actor_meta: dict[str, Any] | None = None,
        section: str = "config",
        summary: str = "Cập nhật cấu hình bot",
        client_changes: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Lưu tạm trong RAM -> ghi Audit Log -> xả lên MongoDB Atlas -> kích hoạt callback."""
        current = await self._repository.get_config(force=True)
        merged = deep_merge(current, patch)
        changes = merge_client_changes(compute_diff(current, merged), client_changes)

        now_iso = datetime.now(timezone.utc).isoformat()
        merged["_id"] = "global"
        merged["updated_at"] = now_iso
        merged["updated_by"] = updated_by

        self._repository.stage_config(merged)

        if changes:
            await self._audit.record(
                actor=updated_by,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="update_config",
                section=section,
                summary=f"{summary} ({len(changes)} thay đổi)",
                changes=changes,
                timestamp=now_iso,
                flush=False,
            )

        await self._sync.flush_pending()

        for callback in self._callbacks:
            try:
                callback(self._repository.current)
            except Exception as exc:
                log.warning("Lỗi callback cập nhật cấu hình: %s", exc)

        return copy.deepcopy(self._repository.current)
