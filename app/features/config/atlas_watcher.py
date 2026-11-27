"""Trình theo dõi đồng bộ cấu hình trực tiếp từ MongoDB Atlas mỗi 5 giây (`atlas_config_watcher`)."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from app.core.cloudflare_tunnel import CloudflareTunnelService
from app.core.settings import build_default_config
from app.core.utils import deep_merge
from app.features.audit.diff import compute_diff, is_sensitive_field, mask_secret_preview
from app.features.audit.service import AuditService
from app.features.config.repository import ConfigRepository
from app.features.config.service import ConfigService

log = logging.getLogger("gemini-bot.atlas-watcher")

DEFAULT_ATLAS_WATCH_INTERVAL_SECONDS = 5.0
ATLAS_EXTERNAL_SYNC_SOURCE = "MongoDB Atlas External Sync"
TUNNEL_PROCESS_KEYS = ("cf_tunnel_enabled", "cf_tunnel_mode", "cf_tunnel_token")
TUNNEL_ALL_KEYS = (
    "cf_tunnel_enabled",
    "cf_tunnel_mode",
    "cf_tunnel_token",
    "tunnel_only_inbound",
    "proxy_headers",
    "warp_outbound_enabled",
)
METADATA_IGNORE_KEYS = {"_id", "updated_at", "updated_by"}


def compute_config_hash(config_doc: dict[str, Any]) -> str:
    """Tính mã băm SHA-256 chuẩn hóa của cấu hình (bỏ qua metadata) để phát hiện thay đổi với chi phí RAM/CPU tối thiểu."""
    comparable = {
        key: val
        for key, val in (config_doc or {}).items()
        if key not in METADATA_IGNORE_KEYS
    }
    serialized = json.dumps(comparable, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _collect_sensitive_diffs(
    old_obj: Any,
    new_obj: Any,
    prefix: str = "",
) -> list[dict[str, Any]]:
    """Phát hiện thay đổi ở các trường nhạy cảm (như `tunnel.cf_tunnel_token`) và trả về diff đã che ký tự bí mật."""
    diffs: list[dict[str, Any]] = []
    if isinstance(old_obj, dict) and isinstance(new_obj, dict):
        for key in sorted(set(old_obj.keys()) | set(new_obj.keys())):
            if not prefix and key in METADATA_IGNORE_KEYS:
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
                diffs.extend(_collect_sensitive_diffs(old_val, new_val, path))
    return diffs


def compute_external_diff(old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Tính danh sách toàn bộ trường thay đổi giữa cấu hình RAM và MongoDB Atlas (tự che giá trị bí mật)."""
    changes = compute_diff(old_cfg, new_cfg)
    existing_fields = {str(item.get("field", "")) for item in changes}
    for secret_diff in _collect_sensitive_diffs(old_cfg, new_cfg):
        if secret_diff["field"] not in existing_fields:
            changes.append(secret_diff)
            existing_fields.add(secret_diff["field"])
    changes.sort(key=lambda item: str(item.get("field", "")))
    return changes


def resolve_effective_tunnel_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Hợp nhất nhánh `tunnel` chuẩn với mọi trường tunnel phẳng đặt ở gốc tài liệu (nếu người dùng sửa tay trên Atlas)."""
    base = dict(cfg.get("tunnel") or {}) if isinstance(cfg.get("tunnel"), dict) else {}
    for key in TUNNEL_ALL_KEYS:
        if key in cfg:
            base[key] = cfg[key]
    return base


def infer_audit_section(changes: list[dict[str, Any]]) -> str:
    """Suy luận chuyên mục Audit Log phù hợp nhất dựa trên danh sách trường vừa thay đổi trên Atlas."""
    fields = [str(item.get("field", "")) for item in changes]
    if not fields:
        return "config"
    if all(f.startswith("tunnel.") or f in TUNNEL_ALL_KEYS for f in fields):
        return "tunnel"
    if all(f.startswith("presence.") for f in fields):
        return "presence"
    if all(f.startswith("ask.") or f.startswith("session.") for f in fields):
        return "ask"
    if all(f.startswith("models.") for f in fields):
        return "models"
    if all(f in ("system_prompt", "ocr_instruction", "append_current_date") for f in fields):
        return "prompt"
    return "config"


class AtlasConfigWatcher:
    """Tác vụ nền kiểm tra `bot_config` trên MongoDB Atlas mỗi 5 giây và tự động hot-reload RAM, Tunnel, Presence & Audit Log."""

    def __init__(
        self,
        *,
        config_repo: ConfigRepository,
        config_service: ConfigService,
        audit_service: AuditService,
        cloudflare_tunnel: Optional[CloudflareTunnelService] = None,
        warp_proxy: Optional[Any] = None,
        maintenance_gate: Optional[Any] = None,
        apply_presence_fn: Optional[Callable[[dict[str, Any]], Awaitable[bool]]] = None,
        poll_interval_seconds: float = DEFAULT_ATLAS_WATCH_INTERVAL_SECONDS,
    ):
        self._config_repo = config_repo
        self._config_service = config_service
        self._audit = audit_service
        self._cloudflare_tunnel = cloudflare_tunnel
        self._warp_proxy = warp_proxy
        self._maintenance_gate = maintenance_gate
        self._apply_presence_fn = apply_presence_fn
        self.poll_interval_seconds = max(0.2, float(poll_interval_seconds))

        self._poll_lock = asyncio.Lock()
        self._last_hash: Optional[str] = None
        self._sync_count: int = 0
        self._last_synced_at: Optional[str] = None
        self._last_error: Optional[str] = None

    def attach_presence_hook(self, fn: Callable[[dict[str, Any]], Awaitable[bool]]) -> None:
        """Gắn callback áp dụng nóng Presence Discord khi tài liệu `bot_config` trên Atlas đổi trường `presence.*`."""
        self._apply_presence_fn = fn

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất trạng thái gọn nhẹ của bộ theo dõi đồng bộ cấu hình trực tiếp từ Atlas."""
        return {
            "interval_seconds": self.poll_interval_seconds,
            "sync_count": self._sync_count,
            "last_synced_at": self._last_synced_at,
            "last_error": self._last_error,
        }

    async def poll_once(self) -> dict[str, Any]:
        """Kiểm tra một chu kỳ `bot_config` trên Atlas; nếu khác `current_config` trong RAM thì nạp nóng toàn bộ hook."""
        async with self._poll_lock:
            if not self._config_repo.connected:
                return {"changed": False, "skipped": True, "reason": "db_disconnected"}
            if self._config_repo.pending_count > 0:
                # Không ghi đè RAM khi Dashboard vừa lưu cấu hình mới và đang chờ xả lên Atlas
                return {"changed": False, "skipped": True, "reason": "local_pending_write"}

            try:
                remote_doc = await self._config_repo.fetch_remote_doc()
            except Exception as exc:
                from app.core.security import redact_secrets

                self._last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=200)
                log.warning("Lỗi truy vấn bot_config từ MongoDB Atlas: %s", self._last_error)
                return {"changed": False, "error": self._last_error}

            if not remote_doc:
                return {"changed": False, "skipped": True, "reason": "missing_remote_doc"}

            current_snapshot = copy.deepcopy(self._config_repo.current_config)
            remote_normalized = deep_merge(build_default_config(), remote_doc)

            current_hash = compute_config_hash(current_snapshot)
            remote_hash = compute_config_hash(remote_normalized)
            if remote_hash == current_hash:
                self._last_hash = remote_hash
                self._last_error = None
                self._config_repo.mark_synced()
                return {"changed": False}

            changes = compute_external_diff(current_snapshot, remote_normalized)
            changed_fields = [str(item.get("field", "")) for item in changes if item.get("field")]

            # 1) Cập nhật ngay `current_config` trong RAM để middleware FastAPI và mọi lệnh bot áp dụng tức thì
            self._config_repo.apply_external_config(remote_normalized)
            self._config_service.notify_callbacks()

            # 2) Áp dụng nóng Cloudflare Tunnel & WARP Outbound nếu bất kỳ trường điều khiển nào thay đổi
            old_tunnel = resolve_effective_tunnel_cfg(current_snapshot)
            new_tunnel = resolve_effective_tunnel_cfg(remote_normalized)
            tunnel_process_changed = any(old_tunnel.get(k) != new_tunnel.get(k) for k in TUNNEL_PROCESS_KEYS)
            tunnel_inbound_changed = bool(old_tunnel.get("tunnel_only_inbound", False)) != bool(
                new_tunnel.get("tunnel_only_inbound", False)
            )
            warp_changed = bool(old_tunnel.get("warp_outbound_enabled", True)) != bool(
                new_tunnel.get("warp_outbound_enabled", True)
            )
            tunnel_reloaded = False

            if self._cloudflare_tunnel is not None:
                if tunnel_process_changed:
                    try:
                        await self._cloudflare_tunnel.apply_config(new_tunnel)
                        tunnel_reloaded = True
                    except Exception as exc:
                        log.warning("Lỗi hot-reload Cloudflare Tunnel từ thay đổi Atlas: %s", exc)
                elif tunnel_inbound_changed:
                    self._cloudflare_tunnel.set_tunnel_only_inbound(
                        bool(new_tunnel.get("tunnel_only_inbound", False))
                    )

            if self._warp_proxy is not None and warp_changed:
                try:
                    await self._warp_proxy.set_enabled(
                        bool(new_tunnel.get("warp_outbound_enabled", True)),
                        force_probe=True,
                    )
                except Exception as exc:
                    log.warning("Lỗi hot-reload Cloudflare WARP từ thay đổi Atlas: %s", exc)

            # 3) Áp dụng nóng Discord Presence & đồng bộ cờ bảo trì DND nếu người dùng chỉnh nhánh `presence` trực tiếp trong Atlas
            presence_reloaded = False
            old_presence = current_snapshot.get("presence") if isinstance(current_snapshot.get("presence"), dict) else {}
            new_presence = remote_normalized.get("presence") if isinstance(remote_normalized.get("presence"), dict) else {}
            old_bot_status = str(old_presence.get("status") or "online").strip().lower()
            new_bot_status = str(new_presence.get("status") or "online").strip().lower()
            if self._maintenance_gate is not None:
                self._maintenance_gate.sync_from_config(new_presence)
                if old_bot_status != new_bot_status:
                    await self._maintenance_gate.record_status_transition(
                        from_status=old_bot_status,
                        to_status=new_bot_status,
                        actor=ATLAS_EXTERNAL_SYNC_SOURCE,
                        actor_type="system",
                        actor_meta={"actor_display_name": ATLAS_EXTERNAL_SYNC_SOURCE},
                        source=ATLAS_EXTERNAL_SYNC_SOURCE,
                        flush=True,
                    )
            if old_presence != new_presence and self._apply_presence_fn is not None:
                try:
                    presence_reloaded = bool(await self._apply_presence_fn(new_presence))
                except Exception as exc:
                    log.warning("Lỗi hot-reload Presence từ thay đổi Atlas: %s", exc)

            # 4) Ghi Audit Log với nguồn `MongoDB Atlas External Sync` ghi rõ các trường vừa được nạp nóng
            if changes:
                preview_fields = ", ".join(changed_fields[:4])
                if len(changed_fields) > 4:
                    preview_fields += f" (+{len(changed_fields) - 4})"
                summary = f"Đồng bộ nóng từ MongoDB Atlas ({len(changes)} thay đổi: {preview_fields})"
                await self._audit.record(
                    actor=ATLAS_EXTERNAL_SYNC_SOURCE,
                    actor_type="system",
                    actor_meta={"actor_display_name": ATLAS_EXTERNAL_SYNC_SOURCE},
                    action="atlas_external_sync",
                    section=infer_audit_section(changes),
                    summary=summary,
                    changes=changes,
                    source=ATLAS_EXTERNAL_SYNC_SOURCE,
                    flush=True,
                )

            self._last_hash = remote_hash
            self._sync_count += 1
            self._last_synced_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None

            log.info(
                "Đã đồng bộ nóng %d thay đổi cấu hình từ MongoDB Atlas (%s) [tunnel_reloaded=%s]",
                len(changes),
                ", ".join(changed_fields) or "metadata",
                tunnel_reloaded,
            )
            return {
                "changed": True,
                "changed_fields": changed_fields,
                "tunnel_reloaded": tunnel_reloaded,
                "presence_reloaded": presence_reloaded,
                "changes": changes,
            }

    async def run_forever(self, is_closed_fn: Callable[[], bool]) -> None:
        """Chạy vòng lặp nền mỗi 5 giây để đồng bộ các chỉnh sửa thủ công trực tiếp trên MongoDB Atlas."""
        while not is_closed_fn():
            try:
                await asyncio.sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                return
            if is_closed_fn():
                return
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("Lỗi vòng lặp atlas_config_watcher: %s", exc)
