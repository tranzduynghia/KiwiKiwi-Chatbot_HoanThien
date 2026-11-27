"""Kho lưu trữ danh sách chấp thuận Điều khoản (`tos_acceptances`) kèm bộ đệm RAM O(1)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.timezone import parse_utc_datetime

log = logging.getLogger("gemini-bot.legal")

MAX_ACCEPTANCE_CACHE_ENTRIES = 10000


class TosAcceptanceRepository:
    """Quản lý collection `tos_acceptances` và bộ đệm `dict[user_id, accepted_version]` trong RAM."""

    def __init__(self) -> None:
        self.collection: Any = None
        self._version_cache: dict[str, str] = {}
        self._memory_records: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        """Cho biết collection `tos_acceptances` đã kết nối MongoDB hay chưa."""
        return self.collection is not None

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `tos_acceptances`, tạo index và nạp bộ đệm phiên bản ToS vào RAM."""
        self.collection = db["tos_acceptances"]
        try:
            await self.collection.create_index("discord_user_id", unique=True)
            await self.collection.create_index([("tos_accepted_at", -1)])
            cursor = self.collection.find({}, {"discord_user_id": 1, "tos_accepted_version": 1}).limit(
                MAX_ACCEPTANCE_CACHE_ENTRIES
            )
            docs = await cursor.to_list(length=MAX_ACCEPTANCE_CACHE_ENTRIES)
            for doc in docs:
                uid = str(doc.get("discord_user_id") or "").strip()
                ver = str(doc.get("tos_accepted_version") or "").strip()
                if uid and ver:
                    self._version_cache[uid] = ver
        except Exception as exc:
            log.warning("Lỗi khởi tạo collection tos_acceptances: %s", exc)

    def get_cached_version(self, user_id: str | int) -> Optional[str]:
        """Đọc tức thì phiên bản ToS người dùng đã chấp thuận từ RAM (0 độ trễ DB)."""
        uid = str(user_id or "").strip()
        if not uid:
            return None
        return self._version_cache.get(uid)

    async def get_accepted_version(self, user_id: str | int) -> Optional[str]:
        """Ưu tiên đọc bộ đệm RAM; nếu miss và đã kết nối DB thì tra cứu MongoDB rồi lưu lại vào cache."""
        uid = str(user_id or "").strip()
        if not uid:
            return None
        cached = self._version_cache.get(uid)
        if cached is not None:
            return cached
        if not self.connected:
            return None
        try:
            doc = await self.collection.find_one({"discord_user_id": uid})
            if doc and doc.get("tos_accepted_version"):
                ver = str(doc["tos_accepted_version"]).strip()
                self._store_cache(uid, ver)
                return ver
        except Exception as exc:
            log.warning("Không đọc được tos_acceptances cho uid=%s: %s", uid, exc)
        return None

    def _store_cache(self, uid: str, version: str) -> None:
        """Lưu phiên bản ToS vào bộ đệm RAM có giới hạn dung lượng bảo vệ container 488MB."""
        self._version_cache[uid] = version
        if len(self._version_cache) > MAX_ACCEPTANCE_CACHE_ENTRIES:
            oldest_key = next(iter(self._version_cache))
            self._version_cache.pop(oldest_key, None)

    async def record_acceptance(
        self,
        *,
        discord_user_id: str | int,
        discord_username: str,
        tos_accepted_version: str,
        consent_source: str = "discord",
        accepted_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Ghi nhận chấp thuận ToS (UTC) lên MongoDB và đồng bộ ngay vào bộ đệm RAM."""
        uid = str(discord_user_id or "").strip()[:64]
        username = " ".join(str(discord_username or uid).split()).strip()[:64] or uid
        version = str(tos_accepted_version or "").strip()[:24]
        source = "web" if str(consent_source or "").strip().lower() == "web" else "discord"
        now_utc = accepted_at or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        doc = {
            "discord_user_id": uid,
            "discord_username": username,
            "tos_accepted_version": version,
            "tos_accepted_at": now_utc,
            "consent_source": source,
        }
        self._store_cache(uid, version)
        self._memory_records[uid] = dict(doc)

        if self.connected:
            try:
                await self.collection.replace_one({"discord_user_id": uid}, doc, upsert=True)
            except Exception as exc:
                log.warning("Không lưu được tos_acceptances lên MongoDB (uid=%s): %s", uid, exc)
        return doc

    async def list_acceptances(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        search: str = "",
    ) -> dict[str, Any]:
        """Truy vấn danh sách chấp thuận ToS có phân trang theo cursor (dựa trên offset/timestamp an toàn) và tìm kiếm."""
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
                    {"discord_username": {"$regex": escaped, "$options": "i"}},
                    {"tos_accepted_version": {"$regex": escaped, "$options": "i"}},
                    {"consent_source": {"$regex": escaped, "$options": "i"}},
                ]
            try:
                total = await self.collection.count_documents(flt)
                raw_docs = (
                    await self.collection.find(flt)
                    .sort("tos_accepted_at", -1)
                    .skip(cursor_offset)
                    .limit(safe_limit + 1)
                    .to_list(length=safe_limit + 1)
                )
            except Exception as exc:
                log.warning("Lỗi truy vấn danh sách tos_acceptances: %s", exc)
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
                    or q_low in str(r.get("discord_username", "")).lower()
                    or q_low in str(r.get("tos_accepted_version", "")).lower()
                    or q_low in str(r.get("consent_source", "")).lower()
                ]
            records.sort(
                key=lambda r: parse_utc_datetime(r.get("tos_accepted_at")) or datetime.min.replace(tzinfo=timezone.utc),
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
