"""Kho lưu trữ & nghiệp vụ Danh sách đen Discord ID (`discord_blacklist`) kèm bộ đệm RAM O(1)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.core.timezone import format_exact_timestamp, parse_utc_datetime
from app.features.audit.service import AuditService

log = logging.getLogger("gemini-bot.blacklist")

DISCORD_SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]+")
MAX_BAN_REASON_CHARS = 300


def validate_discord_snowflake(raw: str | int) -> str:
    """Xác thực Discord User ID dạng số snowflake hợp lệ (17–20 chữ số)."""
    uid = str(raw or "").strip()
    if not DISCORD_SNOWFLAKE_RE.fullmatch(uid):
        raise ValueError("Discord User ID không hợp lệ (phải gồm 17–20 chữ số).")
    return uid


def sanitize_ban_reason(raw: str) -> str:
    """Làm sạch lý do cấm sử dụng thành văn bản thuần an toàn (bắt buộc, tối đa 300 ký tự)."""
    text = str(raw or "")
    text = _HTML_TAG_RE.sub("", text)
    text = _CTRL_CHAR_RE.sub(" ", text)
    cleaned = " ".join(text.split()).strip()[:MAX_BAN_REASON_CHARS]
    if not cleaned:
        raise ValueError("Vui lòng nhập lý do cấm sử dụng (tối đa 300 ký tự).")
    return cleaned


class BlacklistEntryPayload(BaseModel):
    """Payload thêm hoặc cập nhật lý do cấm một Discord User ID."""

    discord_user_id: str = Field(..., min_length=17, max_length=20)
    reason: str = Field(..., min_length=1, max_length=MAX_BAN_REASON_CHARS)
    changes: Optional[dict[str, Any]] = None


class BlacklistRepository:
    """Quản lý collection `discord_blacklist` trên MongoDB và bộ đệm RAM `dict[discord_user_id, reason]`."""

    def __init__(self) -> None:
        self.collection: Any = None
        self._cache: dict[str, str] = {}
        self._memory_records: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        """Cho biết collection `discord_blacklist` đã gắn MongoDB hay chưa."""
        return self.collection is not None

    def get_reason_fast(self, discord_user_id: str | int | None) -> Optional[str]:
        """Tra cứu O(1) không chạm DB xem Discord User ID có nằm trong danh sách đen hay không."""
        uid = str(discord_user_id or "").strip()
        if not uid:
            return None
        return self._cache.get(uid)

    def cached_count(self) -> int:
        """Số lượng Discord User ID hiện đang bị chặn trong bộ đệm RAM."""
        return len(self._cache)

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `discord_blacklist`, tạo index và nạp toàn bộ danh sách đen vào bộ đệm RAM."""
        self.collection = db["discord_blacklist"]
        try:
            await self.collection.create_index("discord_user_id", unique=True)
            await self.collection.create_index([("created_at", -1)])
        except Exception as exc:
            log.warning("Lỗi tạo index cho discord_blacklist: %s", exc)
        await self.reload_cache()

    async def reload_cache(self) -> int:
        """Đọc thuần túy (100% read-only) danh sách `{discord_user_id, reason}` từ MongoDB để đồng bộ bộ đệm RAM."""
        if not self.connected:
            return len(self._cache)
        try:
            docs = await self.collection.find(
                {},
                {"_id": 0, "discord_user_id": 1, "reason": 1},
            ).to_list(length=50000)
            next_cache: dict[str, str] = {}
            for doc in docs:
                uid = str(doc.get("discord_user_id") or "").strip()
                reason = str(doc.get("reason") or "").strip()
                if uid and reason:
                    next_cache[uid] = reason
            self._cache = next_cache
        except Exception as exc:
            log.warning("Không tải lại được bộ đệm discord_blacklist từ MongoDB: %s", exc)
        return len(self._cache)

    async def upsert(
        self,
        *,
        discord_user_id: str | int,
        reason: str,
        banned_by: str,
        created_at: Optional[datetime] = None,
    ) -> tuple[dict[str, Any], Optional[str]]:
        """Thêm mới hoặc cập nhật lý do cấm một Discord User ID trên MongoDB và cập nhật ngay bộ đệm RAM."""
        uid = validate_discord_snowflake(discord_user_id)
        clean_reason = sanitize_ban_reason(reason)
        actor = " ".join(str(banned_by or "admin").split()).strip()[:80] or "admin"
        now_utc = created_at or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        previous_reason = self._cache.get(uid)
        doc = {
            "discord_user_id": uid,
            "reason": clean_reason,
            "banned_by": actor,
            "created_at": now_utc,
        }
        self._cache[uid] = clean_reason
        self._memory_records[uid] = dict(doc)

        if self.connected:
            await self.collection.replace_one({"discord_user_id": uid}, doc, upsert=True)
        return doc, previous_reason

    async def remove(self, discord_user_id: str | int) -> Optional[dict[str, Any]]:
        """Gỡ một Discord User ID khỏi danh sách đen trên MongoDB và xóa ngay khỏi bộ đệm RAM."""
        uid = validate_discord_snowflake(discord_user_id)
        prev_reason = self._cache.pop(uid, None)
        mem_doc = self._memory_records.pop(uid, None)

        existing: Optional[dict[str, Any]] = dict(mem_doc) if isinstance(mem_doc, dict) else None
        if self.connected:
            db_doc = await self.collection.find_one(
                {"discord_user_id": uid},
                {"_id": 0, "discord_user_id": 1, "reason": 1, "banned_by": 1, "created_at": 1},
            )
            if db_doc:
                existing = dict(db_doc)
            res = await self.collection.delete_one({"discord_user_id": uid})
            if not getattr(res, "deleted_count", 0) and existing is None and prev_reason is None:
                return None

        if existing is None and prev_reason is not None:
            existing = {"discord_user_id": uid, "reason": prev_reason}
        return existing

    async def list_entries(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        search: str = "",
    ) -> dict[str, Any]:
        """Truy vấn danh sách đen có tìm kiếm và phân trang theo cursor với projection tối giản."""
        safe_limit = max(1, min(50, int(limit or 15)))
        query_text = " ".join(str(search or "").split()).strip()
        cursor_offset = 0
        if cursor:
            try:
                cursor_offset = max(0, int(cursor))
            except (TypeError, ValueError):
                cursor_offset = 0

        if self.connected:
            flt: dict[str, Any] = {}
            if query_text:
                escaped = re.escape(query_text[:64])
                flt["$or"] = [
                    {"discord_user_id": {"$regex": escaped, "$options": "i"}},
                    {"reason": {"$regex": escaped, "$options": "i"}},
                    {"banned_by": {"$regex": escaped, "$options": "i"}},
                ]
            projection = {
                "_id": 0,
                "discord_user_id": 1,
                "reason": 1,
                "banned_by": 1,
                "created_at": 1,
            }
            try:
                total = await self.collection.count_documents(flt)
                raw_docs = (
                    await self.collection.find(flt, projection)
                    .sort("created_at", -1)
                    .skip(cursor_offset)
                    .limit(safe_limit + 1)
                    .to_list(length=safe_limit + 1)
                )
            except Exception as exc:
                log.warning("Lỗi truy vấn danh sách discord_blacklist: %s", exc)
                raw_docs = []
                total = 0
        else:
            records = list(self._memory_records.values())
            if query_text:
                q_low = query_text.lower()
                records = [
                    r
                    for r in records
                    if q_low in str(r.get("discord_user_id", "")).lower()
                    or q_low in str(r.get("reason", "")).lower()
                    or q_low in str(r.get("banned_by", "")).lower()
                ]
            records.sort(
                key=lambda r: parse_utc_datetime(r.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            total = len(records)
            raw_docs = records[cursor_offset : cursor_offset + safe_limit + 1]

        has_more = len(raw_docs) > safe_limit
        page_docs = raw_docs[:safe_limit]
        next_cursor = str(cursor_offset + safe_limit) if has_more else None
        prev_cursor = str(max(0, cursor_offset - safe_limit)) if cursor_offset > 0 else None

        return {
            "items": page_docs,
            "total": total,
            "limit": safe_limit,
            "cursor": str(cursor_offset),
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "has_more": has_more,
        }


class BlacklistService:
    """Nghiệp vụ quản lý Danh sách đen Discord ID: chặn cấm Admin, đồng bộ RAM tức thì và ghi Audit Log."""

    def __init__(
        self,
        *,
        blacklist_repo: BlacklistRepository,
        permission_service: Any = None,
        maintenance_gate: Any = None,
        audit_service: Optional[AuditService] = None,
    ) -> None:
        self._repo = blacklist_repo
        self._permissions = permission_service
        self._maintenance_gate = maintenance_gate
        self._audit = audit_service

    def set_maintenance_gate(self, gate: Any) -> None:
        """Gắn `MaintenanceGate` để kiểm tra chéo Admin ID từ biến môi trường `ADMIN_DISCORD_ID`."""
        self._maintenance_gate = gate

    def get_ban_reason(self, discord_user_id: str | int | None) -> Optional[str]:
        """Tra cứu O(1) lý do cấm trong bộ đệm RAM."""
        return self._repo.get_reason_fast(discord_user_id)

    async def _is_protected_admin(self, discord_user_id: str) -> bool:
        """Kiểm tra Discord User ID có đang nắm quyền Admin toàn quyền hay không."""
        if self._permissions is not None:
            try:
                perm = await self._permissions.get_permission(discord_user_id, force=False)
                if isinstance(perm, dict) and bool(perm.get("is_admin")):
                    return True
            except Exception as exc:
                log.warning("Lỗi kiểm tra quyền Admin khi xét danh sách đen (uid=%s): %s", discord_user_id, exc)
        if self._maintenance_gate is not None:
            try:
                if await self._maintenance_gate.is_admin_user(discord_user_id):
                    return True
            except Exception as exc:
                log.warning("Lỗi kiểm tra ADMIN_DISCORD_ID khi xét danh sách đen (uid=%s): %s", discord_user_id, exc)
        return False

    async def ban_user(
        self,
        *,
        discord_user_id: str | int,
        reason: str,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Thêm mới hoặc sửa lý do cấm một Discord User ID (cấm chặn tài khoản Admin) và ghi Audit Log `USER_BLACKLISTED`."""
        uid = validate_discord_snowflake(discord_user_id)
        clean_reason = sanitize_ban_reason(reason)

        if await self._is_protected_admin(uid):
            raise ValueError("Không thể thêm Quản trị viên (Admin toàn quyền) vào danh sách đen.")

        doc, previous_reason = await self._repo.upsert(
            discord_user_id=uid,
            reason=clean_reason,
            banned_by=actor,
        )
        is_update = previous_reason is not None
        summary = (
            f"Cập nhật lý do cấm Discord ID {uid}: {clean_reason}"
            if is_update
            else f"Thêm Discord ID {uid} vào danh sách đen: {clean_reason}"
        )
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="USER_BLACKLISTED",
                section="discord_permissions",
                summary=summary,
                changes=[
                    {
                        "field": f"discord_blacklist.{uid}.reason",
                        "before": previous_reason,
                        "after": clean_reason,
                    }
                ],
                source="Dashboard Blacklist",
                extra_fields={
                    "target_discord_user_id": uid,
                    "reason": clean_reason,
                },
                flush=True,
            )
        return doc

    async def unban_user(
        self,
        *,
        discord_user_id: str | int,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Gỡ cấm một Discord User ID và ghi Audit Log `USER_UNBLACKLISTED`."""
        uid = validate_discord_snowflake(discord_user_id)
        removed = await self._repo.remove(uid)
        if removed is None:
            return None

        prev_reason = str(removed.get("reason") or "")
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="USER_UNBLACKLISTED",
                section="discord_permissions",
                summary=f"Gỡ cấm Discord ID {uid} khỏi danh sách đen",
                changes=[
                    {
                        "field": f"discord_blacklist.{uid}.reason",
                        "before": prev_reason,
                        "after": None,
                    }
                ],
                source="Dashboard Blacklist",
                extra_fields={
                    "target_discord_user_id": uid,
                    "previous_reason": prev_reason,
                },
                flush=True,
            )
        return removed

    async def list_entries(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        search: str = "",
        tz_info: Optional[ZoneInfo] = None,
    ) -> dict[str, Any]:
        """Xuất danh sách đen phân trang kèm mốc thời gian `HH:mm:ss DD/MM/YYYY` theo múi giờ IP Admin."""
        page = await self._repo.list_entries(limit=limit, cursor=cursor, search=search)
        formatted_items: list[dict[str, Any]] = []
        for doc in page["items"]:
            created_raw = doc.get("created_at")
            created_iso = created_raw.isoformat() if isinstance(created_raw, datetime) else str(created_raw or "")
            formatted_items.append({
                "discord_user_id": str(doc.get("discord_user_id") or ""),
                "reason": str(doc.get("reason") or ""),
                "banned_by": str(doc.get("banned_by") or "admin"),
                "created_at": created_iso,
                "created_at_formatted": format_exact_timestamp(created_raw, tz_info) if tz_info else created_iso,
            })
        return {
            **page,
            "items": formatted_items,
        }
