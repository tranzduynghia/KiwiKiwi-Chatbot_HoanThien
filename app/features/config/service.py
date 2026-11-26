"""Dịch vụ cập nhật cấu hình động kèm tính toán diff, ghi Audit Log và thông báo callback."""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.core.sync import WriteBehindSyncCoordinator
from app.core.utils import deep_merge
from app.features.audit.diff import compute_diff, is_sensitive_field, mask_secret_preview, merge_client_changes
from app.features.audit.service import AuditService
from app.features.config.repository import ConfigRepository

log = logging.getLogger("gemini-bot.config")

TUNNEL_LEGACY_TOP_LEVEL_KEYS = (
    "cf_tunnel_enabled",
    "cf_tunnel_mode",
    "cf_tunnel_token",
    "tunnel_only_inbound",
)


def _compute_masked_sensitive_diffs(
    old_obj: Any,
    new_obj: Any,
    prefix: str = "",
) -> list[dict[str, Any]]:
    """Phát hiện thay đổi trên trường bí mật phía server và chỉ ghi cờ giữ chỗ tĩnh `••••••••••••`."""
    diffs: list[dict[str, Any]] = []
    if isinstance(old_obj, dict) and isinstance(new_obj, dict):
        for key in sorted(set(old_obj.keys()) | set(new_obj.keys())):
            if not prefix and key in {"_id", "updated_at", "updated_by"}:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            old_val = old_obj.get(key)
            new_val = new_obj.get(key)
            if old_val == new_val:
                continue
            if is_sensitive_field(path):
                diffs.append({
                    "field": path,
                    "before": mask_secret_preview(old_val),
                    "after": mask_secret_preview(new_val),
                    "masked": True,
                })
            elif isinstance(old_val, dict) and isinstance(new_val, dict):
                diffs.extend(_compute_masked_sensitive_diffs(old_val, new_val, path))
    return diffs


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
        self._external_sync_hook: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None

    def on_update(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Đăng ký hàm callback chạy mỗi khi cấu hình toàn cục được cập nhật."""
        self._callbacks.append(callback)

    def set_external_sync_hook(self, hook: Optional[Callable[[], Awaitable[dict[str, Any]]]]) -> None:
        """Gắn hàm đồng bộ nóng từ MongoDB Atlas để mọi lần đọc `force=True` không bỏ sót hook áp dụng nóng."""
        self._external_sync_hook = hook

    def notify_callbacks(self) -> None:
        """Kích hoạt toàn bộ callback lắng nghe thay đổi cấu hình với `current_config` mới nhất trong RAM."""
        for callback in self._callbacks:
            try:
                callback(self._repository.current)
            except Exception as exc:
                log.warning("Lỗi callback cập nhật cấu hình: %s", exc)

    async def get_config(self, force: bool = False) -> dict[str, Any]:
        """Lấy bản sao cấu hình hiện hành (nếu `force=True` và có Atlas watcher thì đồng bộ qua watcher trước)."""
        if force and self._external_sync_hook is not None and self._repository.connected and self._repository.pending_count == 0:
            await self._external_sync_hook()
            return copy.deepcopy(self._repository.current)
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
        current = await self.get_config(force=True)
        merged = deep_merge(current, patch)
        if "tunnel" in patch:
            # Khi lưu cấu hình tunnel chuẩn từ Dashboard, dọn mọi khóa tunnel phẳng có thể từng được sửa tay trên Atlas
            for stray_key in TUNNEL_LEGACY_TOP_LEVEL_KEYS:
                merged.pop(stray_key, None)

        server_changes = compute_diff(current, merged) + _compute_masked_sensitive_diffs(current, merged)
        changes = merge_client_changes(server_changes, client_changes)

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
        self.notify_callbacks()

        return copy.deepcopy(self._repository.current)
