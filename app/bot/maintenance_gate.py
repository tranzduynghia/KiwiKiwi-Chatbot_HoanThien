"""Cổng bảo trì chế độ Không làm phiền (DND Maintenance Mode Gate) cho Discord Bot & Dashboard."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import discord

from app.features.audit.service import AuditService
from app.features.config.repository import ConfigRepository

log = logging.getLogger("gemini-bot.maintenance")

VALID_BOT_STATUSES = frozenset({"online", "idle", "dnd", "invisible"})
DND_BLOCK_NOTICE = "⛔ Bot hiện đang ở chế độ Không làm phiền (DND) để bảo trì hệ thống. Vui lòng thử lại sau!"
DND_BLOCK_LOG_THROTTLE_SECONDS = 60.0
DND_BLOCK_THROTTLE_MAX_USERS = 2000


def _parse_admin_discord_ids(raw: str) -> frozenset[str]:
    """Tách danh sách Discord User ID của Admin từ chuỗi môi trường `ADMIN_DISCORD_ID` (hỗ trợ 1 hoặc nhiều ID cách nhau dấu phẩy)."""
    tokens = [tok.strip() for tok in re.split(r"[,\s;]+", str(raw or "")) if tok.strip()]
    return frozenset(tok for tok in tokens if tok.isdigit())


def _normalize_status(raw: Any) -> str:
    """Chuẩn hóa khóa trạng thái bot về một trong `online` | `idle` | `dnd` | `invisible`."""
    value = str(raw or "online").strip().lower()
    return value if value in VALID_BOT_STATUSES else "online"


def _describe_interaction_target(interaction: Any) -> str:
    """Xác định tên lệnh slash hoặc custom_id của nút/thành phần đang được gọi."""
    cmd = getattr(interaction, "command", None)
    cmd_name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None)
    if cmd_name:
        return f"/{cmd_name}"
    data = getattr(interaction, "data", None)
    if isinstance(data, dict):
        if data.get("name"):
            return f"/{data['name']}"
        if data.get("custom_id"):
            return f"component:{str(data['custom_id'])[:64]}"
    channel_id = getattr(interaction, "channel_id", None)
    if channel_id:
        return f"channel:{channel_id}"
    return "interaction:unknown"


class MaintenanceGate:
    """Giữ trạng thái `bot_status` trong RAM đồng bộ thời gian thực với MongoDB và chặn người dùng thường khi bật DND."""

    def __init__(
        self,
        *,
        config_repo: ConfigRepository,
        audit_service: AuditService,
        permission_service: Any = None,
        blacklist_repo: Any = None,
        admin_discord_id: str = "",
        block_log_throttle_seconds: float = DND_BLOCK_LOG_THROTTLE_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        self._config_repo = config_repo
        self._audit = audit_service
        self._permission_service = permission_service
        self._blacklist_repo = blacklist_repo
        self._configured_admin_ids = _parse_admin_discord_ids(admin_discord_id)
        self._block_log_throttle_seconds = max(1.0, float(block_log_throttle_seconds))
        self._monotonic = monotonic_fn

        self._bot_status: str = _normalize_status(config_repo.presence_cfg().get("status"))
        self._last_config_status: str = self._bot_status
        self._last_block_audit_at: dict[str, float] = {}
        self._last_blacklist_audit_at: dict[str, float] = {}
        self._blocked_total: int = 0
        self._blacklist_blocked_total: int = 0
        self._admin_bypass_total: int = 0

    def set_blacklist_repo(self, blacklist_repo: Any) -> None:
        """Gắn `BlacklistRepository` để kiểm tra danh sách đen O(1) ưu tiên cao nhất trước DND và ToS."""
        self._blacklist_repo = blacklist_repo

    @staticmethod
    def format_blacklist_notice(reason: str) -> str:
        """Soạn thông báo chặn người dùng nằm trong danh sách đen kèm lý do cấm."""
        clean_reason = str(reason or "Vi phạm quy định sử dụng bot.").strip()
        return (
            "🚫 **Tài khoản của bạn đã bị hạn chế sử dụng KiwiKiwi Bot.**\n"
            f"**Lý do:** {clean_reason}"
        )

    @property
    def bot_status(self) -> str:
        """Trạng thái hiển thị hiện tại của bot được lưu đệm trong RAM (`online`, `idle`, `dnd`, `invisible`)."""
        return self._bot_status

    @property
    def is_dnd(self) -> bool:
        """Trả về `True` khi bot đang bật chế độ Không làm phiền (DND) — khóa bảo trì hệ thống.

        Đọc trực tiếp `presence.status` hiện hành (nguồn duy nhất) rồi đồng bộ lại cờ RAM,
        tránh cờ cũ khi cấu hình được đổi qua Dashboard, `/config` hoặc `atlas_watcher`.
        """
        try:
            live = _normalize_status(self._config_repo.presence_cfg().get("status"))
        except Exception:
            return self._bot_status == "dnd"
        if live != self._last_config_status:
            # Cấu hình presence vừa đổi (Dashboard, /config, atlas_watcher) -> nhận ngay giá trị mới
            self._last_config_status = live
            self._bot_status = live
        return self._bot_status == "dnd"

    def sync_from_config(self, cfg_or_presence: Optional[dict[str, Any]] = None) -> str:
        """Cập nhật ngay cờ trạng thái trong RAM từ tài liệu cấu hình hoặc nhánh `presence`."""
        source = cfg_or_presence if isinstance(cfg_or_presence, dict) else self._config_repo.current_config
        presence = source.get("presence") if isinstance(source.get("presence"), dict) else source
        if isinstance(presence, dict) and "status" in presence:
            self._bot_status = _normalize_status(presence.get("status"))
        else:
            self._bot_status = _normalize_status(self._config_repo.presence_cfg().get("status"))
        self._last_config_status = _normalize_status(self._config_repo.presence_cfg().get("status"))
        return self._bot_status

    def status_snapshot(self) -> dict[str, Any]:
        """Xuất dữ liệu telemetry chế độ bảo trì DND cho `/api/status` và Dashboard."""
        return {
            "bot_status": self._bot_status,
            "dnd_maintenance": self.is_dnd,
            "blocked_count": self._blocked_total,
            "admin_bypass_count": self._admin_bypass_total,
        }

    def _effective_admin_ids(self) -> frozenset[str]:
        """Đọc tập hợp `ADMIN_DISCORD_ID` từ cấu hình khởi tạo và biến môi trường hiện hành."""
        env_ids = _parse_admin_discord_ids(os.getenv("ADMIN_DISCORD_ID", ""))
        if env_ids:
            return self._configured_admin_ids | env_ids
        return self._configured_admin_ids

    async def is_admin_user(self, user_id: int | str | None) -> bool:
        """Kiểm tra Discord User ID có khớp `ADMIN_DISCORD_ID` (hoặc là Admin trong `discord_permissions`) để vượt khóa DND hay không."""
        uid = str(user_id or "").strip()
        if not uid:
            return False
        admin_ids = self._effective_admin_ids()
        if uid in admin_ids:
            return True
        if self._permission_service is not None:
            try:
                perm = await self._permission_service.get_permission(uid, force=False)
                if isinstance(perm, dict) and bool(perm.get("is_admin")):
                    return True
            except Exception as exc:
                log.warning("Không kiểm tra được quyền Admin Discord khi xét khóa DND (uid=%s): %s", uid, exc)
        return False

    def purge_stale_throttles(self) -> int:
        """Dọn các mốc rate-limit chặn DND & Blacklist đã hết hạn để tiết kiệm RAM cho container 488MB."""
        now = self._monotonic()
        cutoff = now - self._block_log_throttle_seconds
        removed = 0
        for bucket in (self._last_block_audit_at, self._last_blacklist_audit_at):
            stale_keys = [uid for uid, ts in bucket.items() if ts < cutoff]
            for uid in stale_keys:
                bucket.pop(uid, None)
            removed += len(stale_keys)
        return removed

    def _should_record_throttled(self, bucket: dict[str, float], uid: str) -> bool:
        """Giới hạn tối đa 1 bản ghi Audit Log MongoDB mỗi người dùng trong mỗi 60 giây nhằm chống spam làm phình DB/RAM."""
        now = self._monotonic()
        last_ts = bucket.get(uid)
        if last_ts is not None and (now - last_ts) < self._block_log_throttle_seconds:
            return False
        bucket[uid] = now
        if len(bucket) > DND_BLOCK_THROTTLE_MAX_USERS:
            self.purge_stale_throttles()
            while len(bucket) > DND_BLOCK_THROTTLE_MAX_USERS:
                oldest_uid = next(iter(bucket))
                bucket.pop(oldest_uid, None)
        return True

    def _should_record_block_audit(self, uid: str) -> bool:
        """Giới hạn tối đa 1 bản ghi Audit Log chặn DND mỗi người dùng trong mỗi 60 giây."""
        return self._should_record_throttled(self._last_block_audit_at, uid)

    def _should_record_blacklist_audit(self, uid: str) -> bool:
        """Giới hạn tối đa 1 bản ghi Audit Log chặn Blacklist mỗi người dùng trong mỗi 60 giây."""
        return self._should_record_throttled(self._last_blacklist_audit_at, uid)

    def _get_blacklist_reason(self, user_id: int | str | None) -> Optional[str]:
        """Tra cứu O(1) trong bộ đệm RAM xem Discord User ID có bị blacklist không."""
        if self._blacklist_repo is None or user_id is None:
            return None
        return self._blacklist_repo.get_reason_fast(user_id)

    async def _log_blacklist_blocked(self, user: Any, command_or_channel: str, reason: str) -> None:
        """Ghi log ứng dụng `[BLACKLIST_BLOCK]` và ghi sự kiện Audit MongoDB có giới hạn tần suất 60 giây/user."""
        uid = str(getattr(user, "id", "") or "unknown")
        display_name = str(
            getattr(user, "display_name", "")
            or getattr(user, "name", "")
            or f"user:{uid}"
        )
        log.info(
            "[BLACKLIST_BLOCK] user_id=%s command_or_channel=%s reason=%s",
            uid,
            command_or_channel,
            reason[:120],
        )
        if not self._should_record_blacklist_audit(uid):
            return
        try:
            await self._audit.record(
                actor=f"{display_name} (discord:{uid})",
                actor_type="discord",
                actor_meta={
                    "actor_display_name": display_name,
                    "actor_discord_id": uid,
                },
                action="BLACKLIST_REQUEST_BLOCKED",
                section="discord_permissions",
                summary=f"Chặn yêu cầu {command_or_channel} từ tài khoản bị blacklist ({uid})",
                changes=[
                    {
                        "field": "blacklist_gate.blocked_target",
                        "before": "allowed",
                        "after": command_or_channel,
                    }
                ],
                source="Discord Blacklist Gate",
                extra_fields={
                    "user_id": uid,
                    "command_or_channel": command_or_channel,
                    "reason": reason,
                },
                flush=True,
            )
        except Exception as exc:
            log.warning("Không ghi được Audit Log cho sự kiện [BLACKLIST_BLOCK] (uid=%s): %s", uid, exc)

    async def record_status_transition(
        self,
        *,
        from_status: str,
        to_status: str,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        source: str = "Dashboard",
        flush: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Ghi nhận Audit Log (`action="BOT_STATUS_CHANGE"`) và log INFO cấu trúc mỗi khi trạng thái bot chuyển sang/ra khỏi DND hoặc đổi trạng thái."""
        old_s = _normalize_status(from_status)
        new_s = _normalize_status(to_status)
        self._bot_status = new_s
        if old_s == new_s:
            return None

        now_iso = datetime.now(timezone.utc).isoformat()
        log.info(
            "[BOT_STATUS_CHANGE] from_status=%s to_status=%s maintenance_mode=%s actor=%s source=%s",
            old_s,
            new_s,
            new_s == "dnd",
            actor,
            source,
        )

        if new_s == "dnd":
            summary = f"Bật chế độ bảo trì DND ({old_s} → dnd) — đã tạm khóa /ask & phiên AI của người dùng thường"
        elif old_s == "dnd":
            summary = f"Tắt chế độ bảo trì DND (dnd → {new_s}) — đã mở khóa lại /ask & phiên AI"
        else:
            summary = f"Đổi trạng thái Bot ({old_s} → {new_s})"

        return await self._audit.record(
            actor=actor,
            actor_type=actor_type,
            actor_meta=actor_meta,
            action="BOT_STATUS_CHANGE",
            section="presence",
            summary=summary,
            changes=[
                {"field": "presence.status", "before": old_s, "after": new_s},
                {"field": "maintenance_mode", "before": old_s == "dnd", "after": new_s == "dnd"},
            ],
            timestamp=now_iso,
            source=source,
            extra_fields={
                "from_status": old_s,
                "to_status": new_s,
            },
            flush=flush,
        )

    async def _log_blocked_request(self, user: Any, command_or_channel: str) -> None:
        """Ghi log ứng dụng `[DND_BLOCK]` và ghi sự kiện Audit MongoDB có giới hạn tần suất theo từng user (60s)."""
        uid = str(getattr(user, "id", "") or "unknown")
        display_name = str(
            getattr(user, "display_name", "")
            or getattr(user, "name", "")
            or f"user:{uid}"
        )
        log.info(
            "[DND_BLOCK] user_id=%s command_or_channel=%s status=dnd",
            uid,
            command_or_channel,
        )
        if not self._should_record_block_audit(uid):
            return
        try:
            await self._audit.record(
                actor=f"{display_name} (discord:{uid})",
                actor_type="discord",
                actor_meta={
                    "actor_display_name": display_name,
                    "actor_discord_id": uid,
                },
                action="DND_REQUEST_BLOCKED",
                section="system",
                summary=f"Chặn yêu cầu {command_or_channel} khi đang bật chế độ bảo trì DND",
                changes=[
                    {
                        "field": "dnd_gate.blocked_target",
                        "before": "allowed",
                        "after": command_or_channel,
                    }
                ],
                source="Discord DND Maintenance Gate",
                extra_fields={
                    "user_id": uid,
                    "command_or_channel": command_or_channel,
                },
                flush=True,
            )
        except Exception as exc:
            log.warning("Không ghi được Audit Log cho sự kiện [DND_BLOCK] (uid=%s): %s", uid, exc)

    async def check_interaction(self, interaction: discord.Interaction) -> bool:
        """Cổng kiểm soát toàn cục ưu tiên cao nhất: Blacklist (O(1) RAM) -> DND bảo trì."""
        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        target = _describe_interaction_target(interaction)

        # 1) Kiểm tra Danh sách đen (ưu tiên cao nhất — chạy trước cả DND, ToS và Gemini)
        ban_reason = self._get_blacklist_reason(user_id)
        if ban_reason is not None:
            self._blacklist_blocked_total += 1
            notice = self.format_blacklist_notice(ban_reason)
            try:
                response = getattr(interaction, "response", None)
                if response is not None and not response.is_done():
                    await response.send_message(notice, ephemeral=True)
                else:
                    followup = getattr(interaction, "followup", None)
                    if followup is not None:
                        await followup.send(notice, ephemeral=True)
            except discord.HTTPException as exc:
                log.warning("Không gửi được thông báo blacklist (ephemeral) cho user_id=%s: %s", user_id, exc)
            except Exception as exc:
                log.warning("Lỗi gửi phản hồi chặn blacklist cho user_id=%s: %s", user_id, exc)
            await self._log_blacklist_blocked(user, target, ban_reason)
            return False

        # 2) Kiểm tra chế độ bảo trì Không làm phiền (DND)
        if not self.is_dnd:
            return True

        if await self.is_admin_user(user_id):
            self._admin_bypass_total += 1
            log.info(
                "[DND_ADMIN_BYPASS] user_id=%s command_or_channel=%s status=dnd",
                user_id,
                target,
            )
            return True

        self._blocked_total += 1
        try:
            response = getattr(interaction, "response", None)
            if response is not None and not response.is_done():
                await response.send_message(DND_BLOCK_NOTICE, ephemeral=True)
            else:
                followup = getattr(interaction, "followup", None)
                if followup is not None:
                    await followup.send(DND_BLOCK_NOTICE, ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("Không gửi được thông báo bảo trì DND (ephemeral) cho user_id=%s: %s", user_id, exc)
        except Exception as exc:
            log.warning("Lỗi gửi phản hồi chặn DND cho user_id=%s: %s", user_id, exc)

        await self._log_blocked_request(user, target)
        return False

    async def check_message(self, message: discord.Message) -> bool:
        """Cổng kiểm soát cho sự kiện `on_message` theo thứ tự ưu tiên: Blacklist -> DND bảo trì."""
        author = getattr(message, "author", None)
        user_id = getattr(author, "id", None)
        channel_id = getattr(getattr(message, "channel", None), "id", None)
        target = f"channel:{channel_id}" if channel_id else "message:direct"

        # 1) Kiểm tra Danh sách đen trước cả DND và ToS
        ban_reason = self._get_blacklist_reason(user_id)
        if ban_reason is not None:
            self._blacklist_blocked_total += 1
            notice = self.format_blacklist_notice(ban_reason)
            try:
                reply_fn = getattr(message, "reply", None)
                if callable(reply_fn):
                    await reply_fn(
                        notice,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                        delete_after=30,
                    )
            except discord.HTTPException as exc:
                log.warning("Không gửi được phản hồi chặn blacklist trong on_message cho user_id=%s: %s", user_id, exc)
            except Exception as exc:
                log.warning("Lỗi gửi phản hồi chặn blacklist trong on_message cho user_id=%s: %s", user_id, exc)
            await self._log_blacklist_blocked(author, target, ban_reason)
            return False

        # 2) Kiểm tra chế độ bảo trì DND
        if not self.is_dnd:
            return True

        if await self.is_admin_user(user_id):
            self._admin_bypass_total += 1
            log.info(
                "[DND_ADMIN_BYPASS] user_id=%s command_or_channel=%s status=dnd",
                user_id,
                target,
            )
            return True

        self._blocked_total += 1
        try:
            reply_fn = getattr(message, "reply", None)
            if callable(reply_fn):
                await reply_fn(
                    DND_BLOCK_NOTICE,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                    delete_after=15,
                )
        except discord.HTTPException as exc:
            log.warning("Không gửi được phản hồi chặn DND trong on_message cho user_id=%s: %s", user_id, exc)
        except Exception as exc:
            log.warning("Lỗi gửi phản hồi chặn DND trong on_message cho user_id=%s: %s", user_id, exc)

        await self._log_blocked_request(author, target)
        return False
