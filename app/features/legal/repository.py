"""Kho lưu trữ tài liệu pháp lý đọc theo yêu cầu (`legal_documents`, KHÔNG đệm toàn bộ HTML trong RAM) và danh sách chấp thuận ToS (`tos_acceptances`)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.timezone import parse_utc_datetime
from app.features.legal.defaults import (
    ALL_CMS_DOC_KEYS,
    LEGAL_DOC_KEYS,
    LEGAL_DOC_LABELS,
    TEMP_DRAFT_LABEL,
    build_default_legal_document,
)
from app.features.legal.sanitizer import sanitize_legal_html

log = logging.getLogger("gemini-bot.legal")

MAX_ACCEPTANCE_CACHE_ENTRIES = 10000
_DISCORD_AVATAR_URL_RE = re.compile(
    r"^https://cdn\.discordapp\.com/avatars/\d{17,20}/[A-Za-z0-9_.-]+(?:\.(?:png|jpg|jpeg|webp|gif))?(?:\?size=\d{2,4})?$",
    re.IGNORECASE,
)
_DISCORD_ID_RE = re.compile(r"^\d{17,20}$")
_DISCORD_AVATAR_HASH_RE = re.compile(r"^[A-Za-z0-9_]{4,64}$")


def validate_discord_avatar_url(
    raw_url: Optional[str],
    *,
    discord_user_id: Optional[str] = None,
    discord_avatar: Optional[str] = None,
) -> Optional[str]:
    """Xác thực URL ảnh đại diện Discord CDN (`^https://cdn.discordapp.com/avatars/`); trả về `None` nếu không hợp lệ hoặc tài khoản cục bộ."""
    candidate = str(raw_url or "").strip()
    if candidate and _DISCORD_AVATAR_URL_RE.fullmatch(candidate):
        return candidate
    uid = str(discord_user_id or "").strip()
    avatar_hash = str(discord_avatar or "").strip()
    if _DISCORD_ID_RE.fullmatch(uid) and _DISCORD_AVATAR_HASH_RE.fullmatch(avatar_hash):
        built = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=64"
        if _DISCORD_AVATAR_URL_RE.fullmatch(built):
            return built
    return None


class LegalDocumentRepository:
    """Truy vấn và lưu nội dung Rich-Text của 5 trang pháp lý trực tiếp trên MongoDB `legal_documents` theo yêu cầu (0 full-HTML RAM cache)."""

    def __init__(self) -> None:
        self.collection: Any = None
        # Chỉ dùng dự phòng khi chưa kết nối MongoDB (chế độ offline/unit test)
        self._offline_fallback: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        """Cho biết collection `legal_documents` đã gắn MongoDB hay chưa."""
        return self.collection is not None

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `legal_documents` và chỉ chèn trang mặc định còn thiếu bằng `$setOnInsert` (không ghi đè bản hiện có)."""
        self.collection = db["legal_documents"]
        try:
            for key in LEGAL_DOC_KEYS:
                default_doc = build_default_legal_document(key)
                await self.collection.update_one(
                    {"_id": key},
                    {"$setOnInsert": default_doc},
                    upsert=True,
                )
            # Giải phóng hoàn toàn bản dự phòng RAM ngay khi đã gắn MongoDB
            self._offline_fallback.clear()
        except Exception as exc:
            log.warning("Lỗi khởi tạo collection legal_documents trên MongoDB: %s", exc)

    async def get_document(self, doc_key: str) -> dict[str, Any]:
        """Đọc thuần túy (100% read-only) nội dung Rich-Text của 1 trang pháp lý từ MongoDB `legal_documents`."""
        from app.features.legal.defaults import DEFAULT_LEGAL_AUTHOR_NAME

        key = str(doc_key or "").strip().lower()
        if key not in ALL_CMS_DOC_KEYS:
            raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")

        default_doc = build_default_legal_document(key)
        raw: Optional[dict[str, Any]] = None
        if self.connected:
            try:
                raw = await self.collection.find_one({"_id": key})
                if raw is None:
                    raw = default_doc
            except Exception as exc:
                log.warning("Lỗi đọc tài liệu pháp lý '%s' từ MongoDB: %s", key, exc)
                raw = default_doc
        else:
            raw = self._offline_fallback.get(key) or default_doc

        raw_body = str(raw.get("body") or default_doc["body"])
        raw_updated = raw.get("updated_at")
        raw_updated_by = str(raw.get("updated_by") or "system-default").strip()
        raw_author_name = str(raw.get("updated_by_name") or "").strip()
        if not raw_author_name:
            raw_author_name = (
                DEFAULT_LEGAL_AUTHOR_NAME
                if raw_updated_by in ("", "system-default")
                else raw_updated_by
            )
        avatar_url = validate_discord_avatar_url(raw.get("updated_by_avatar_url"))
        return {
            "key": key,
            "label": LEGAL_DOC_LABELS.get(key, TEMP_DRAFT_LABEL),
            "title": str(raw.get("title") or default_doc["title"]).strip(),
            "subtitle": str(raw.get("subtitle") if raw.get("subtitle") is not None else default_doc["subtitle"]).strip(),
            "body": sanitize_legal_html(raw_body),
            "updated_at": str(raw_updated).strip() if raw_updated else None,
            "updated_by": raw_updated_by,
            "updated_by_name": raw_author_name,
            "updated_by_avatar_url": avatar_url,
        }

    async def save_document(
        self,
        *,
        doc_key: str,
        title: str,
        subtitle: str,
        body_html: str,
        updated_by: str,
        updated_by_name: Optional[str] = None,
        updated_by_avatar_url: Optional[str] = None,
        updated_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Làm sạch HTML bằng allowlist và ghi thẳng xuống MongoDB `legal_documents` kèm tác giả & avatar."""
        key = str(doc_key or "").strip().lower()
        if key not in ALL_CMS_DOC_KEYS:
            raise ValueError(f"Tài liệu pháp lý không hợp lệ: {doc_key}")

        now_iso = updated_at or datetime.now(timezone.utc).isoformat()
        clean_html = sanitize_legal_html(body_html)
        clean_actor = str(updated_by or "dashboard").strip()[:64]
        clean_author_name = " ".join(str(updated_by_name or clean_actor or "Admin").split()).strip()[:64] or "Admin"
        clean_avatar_url = validate_discord_avatar_url(updated_by_avatar_url)
        doc = {
            "_id": key,
            "key": key,
            "label": LEGAL_DOC_LABELS.get(key, TEMP_DRAFT_LABEL),
            "title": str(title).strip()[:160],
            "subtitle": str(subtitle).strip()[:240],
            "body": clean_html,
            "updated_at": now_iso,
            "updated_by": clean_actor,
            "updated_by_name": clean_author_name,
            "updated_by_avatar_url": clean_avatar_url,
        }
        if self.connected:
            await self.collection.replace_one({"_id": key}, doc, upsert=True)
        else:
            self._offline_fallback[key] = dict(doc)
        return doc


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

    async def revoke_acceptance(self, discord_user_id: str | int) -> dict[str, Any]:
        """Thu hồi bản ghi chấp thuận ToS của 1 người dùng trên MongoDB và xóa ngay khỏi bộ đệm RAM."""
        uid = str(discord_user_id or "").strip()[:64]
        if not uid:
            raise ValueError("Discord User ID không hợp lệ.")

        previous_version: Optional[str] = self._version_cache.pop(uid, None)
        mem_doc = self._memory_records.pop(uid, None)
        previous_username: Optional[str] = (
            str(mem_doc.get("discord_username") or "").strip() if isinstance(mem_doc, dict) else None
        )
        if not previous_version and isinstance(mem_doc, dict):
            previous_version = str(mem_doc.get("tos_accepted_version") or "").strip() or None

        deleted_db = False
        if self.connected:
            try:
                existing = await self.collection.find_one(
                    {"discord_user_id": uid},
                    {"_id": 0, "tos_accepted_version": 1, "discord_username": 1},
                )
                if existing:
                    if not previous_version:
                        previous_version = str(existing.get("tos_accepted_version") or "").strip() or None
                    if not previous_username:
                        previous_username = str(existing.get("discord_username") or "").strip() or None
                res = await self.collection.delete_one({"discord_user_id": uid})
                deleted_db = bool(getattr(res, "deleted_count", 0))
            except Exception as exc:
                log.warning("Không xóa được tos_acceptances trên MongoDB (uid=%s): %s", uid, exc)
                raise

        revoked = bool(deleted_db or previous_version or mem_doc is not None)
        return {
            "discord_user_id": uid,
            "revoked": revoked,
            "previous_version": previous_version,
            "discord_username": previous_username,
        }

    async def list_acceptances(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        search: str = "",
    ) -> dict[str, Any]:
        """Truy vấn danh sách chấp thuận ToS có phân trang theo cursor và tìm kiếm."""
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
            acceptance_projection = {
                "_id": 0,
                "discord_user_id": 1,
                "discord_username": 1,
                "tos_accepted_version": 1,
                "tos_accepted_at": 1,
                "consent_source": 1,
            }
            try:
                total = await self.collection.count_documents(flt)
                raw_docs = (
                    await self.collection.find(flt, acceptance_projection)
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
