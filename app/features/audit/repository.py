"""Kho lưu trữ Audit Log kết hợp bộ đệm RAM chờ đồng bộ lên MongoDB collection `audit_logs`."""
from __future__ import annotations

import copy
import logging
import math
from datetime import date, datetime
from typing import Any, Optional

log = logging.getLogger("gemini-bot.audit")

MAX_PENDING_AUDITS = 500
DEFAULT_AUDIT_PAGE_LIMIT = 5
MAX_AUDIT_PAGE_LIMIT = 20


def _serialize_json_value(value: Any) -> Any:
    """Chuyển đệ quy các kiểu dữ liệu BSON/Python (ObjectId, datetime, bytes...) sang JSON an toàn cho FastAPI."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _serialize_json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_json_value(v) for v in value]
    # Hỗ trợ bson.ObjectId, Decimal128 và các kiểu tùy chỉnh khác bằng chuỗi
    return str(value)


def _serialize_audit_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Chuẩn hóa một bản ghi Audit Log từ MongoDB: `_id` -> chuỗi, `datetime` -> ISO và bảo đảm có trường `at`."""
    clean = _serialize_json_value(doc)
    if "_id" in doc and doc["_id"] is not None:
        clean["_id"] = str(doc["_id"])
    if not clean.get("at"):
        fallback_ts = clean.get("timestamp") or clean.get("created_at") or ""
        if fallback_ts:
            clean["at"] = str(fallback_ts)
    return clean


def _normalize_section_filter(section: Optional[str]) -> str:
    """Chuẩn hóa bộ lọc chuyên mục (coi rỗng hoặc `all` là không lọc)."""
    sec = (section or "").strip()
    if sec.lower() in ("", "all", "tất cả"):
        return ""
    return sec


class AuditRepository:
    """Quản lý đọc/ghi và hàng đợi RAM tạm thời cho bản ghi Audit Log."""

    sync_key = "audits"

    def __init__(self):
        self.collection: Any = None
        self._pending_audits: list[dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        """Kiểm tra collection `audit_logs` đã sẵn sàng hay chưa."""
        return self.collection is not None

    @property
    def pending_count(self) -> int:
        """Số bản ghi Audit Log đang chờ xả lên MongoDB Atlas."""
        return len(self._pending_audits)

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `audit_logs` và tạo các index phục vụ truy vấn nhanh."""
        self.collection = db["audit_logs"]
        try:
            await self.collection.create_index([("at", -1)])
            await self.collection.create_index([("section", 1), ("at", -1)])
        except Exception as exc:
            log.warning("Không tạo được index cho audit_logs: %s", exc)

    def enqueue(self, doc: dict[str, Any]) -> None:
        """Đưa bản ghi Audit Log vào hàng đợi RAM (giới hạn tối đa 500 bản ghi khi mất mạng)."""
        self._pending_audits.append(copy.deepcopy(doc))
        if len(self._pending_audits) > MAX_PENDING_AUDITS:
            self._pending_audits = self._pending_audits[-MAX_PENDING_AUDITS:]

    async def flush_pending(self) -> int:
        """Đẩy toàn bộ Audit Log đang chờ trong RAM lên MongoDB theo lô (`insert_many`) và giải phóng ngay."""
        if not self.connected or not self._pending_audits:
            return 0
        batch = []
        for item in self._pending_audits:
            audit_doc = copy.deepcopy(item)
            audit_doc.pop("pending", None)
            batch.append(audit_doc)

        insert_many_fn = getattr(self.collection, "insert_many", None)
        if callable(insert_many_fn) and len(batch) > 1:
            await insert_many_fn(batch, ordered=False)
            flushed = len(batch)
            self._pending_audits.clear()
            return flushed

        flushed = 0
        while self._pending_audits:
            audit_doc = copy.deepcopy(self._pending_audits[0])
            audit_doc.pop("pending", None)
            await self.collection.insert_one(audit_doc)
            self._pending_audits.pop(0)
            flushed += 1
        return flushed

    async def list_logs_paginated(
        self,
        *,
        page: int = 1,
        limit: int = DEFAULT_AUDIT_PAGE_LIMIT,
        section: Optional[str] = None,
    ) -> dict[str, Any]:
        """Phân trang phía server với `.sort('at', -1).skip().limit()` và `count_documents()`, tránh nạp toàn bộ collection vào RAM."""
        sec = _normalize_section_filter(section)
        safe_limit = max(1, min(MAX_AUDIT_PAGE_LIMIT, int(limit)))
        requested_page = max(1, int(page))

        pending_items: list[dict[str, Any]] = []
        for doc in reversed(self._pending_audits):
            if not sec or doc.get("section") == sec:
                item = _serialize_audit_doc(doc)
                item["pending"] = True
                pending_items.append(item)

        query: dict[str, Any] = {"section": sec} if sec else {}
        db_total = 0
        if self.connected:
            try:
                db_total = int(await self.collection.count_documents(query))
            except Exception as exc:
                log.warning("Không đếm được audit_logs từ MongoDB: %s", exc)

        total_items = len(pending_items) + db_total
        total_pages = max(1, math.ceil(total_items / safe_limit)) if total_items > 0 else 1
        safe_page = min(requested_page, total_pages)

        start_idx = (safe_page - 1) * safe_limit
        end_idx = start_idx + safe_limit
        page_items = pending_items[start_idx:end_idx]

        remaining = safe_limit - len(page_items)
        if remaining > 0 and self.connected and db_total > 0:
            db_skip = max(0, start_idx - len(pending_items))
            try:
                cursor = (
                    self.collection.find(query)
                    .sort("at", -1)
                    .skip(db_skip)
                    .limit(remaining)
                )
                db_items = await cursor.to_list(length=remaining)
                page_items.extend(_serialize_audit_doc(doc) for doc in db_items)
            except Exception as exc:
                log.warning("Không đọc được trang audit_logs từ MongoDB: %s", exc)

        return {
            "items": page_items,
            "page": safe_page,
            "limit": safe_limit,
            "total_items": total_items,
            "total_pages": total_pages,
        }

    async def list_logs(self, limit: int = 100, section: Optional[str] = None) -> list[dict[str, Any]]:
        """Lấy danh sách Audit Log (ưu tiên bản ghi đang chờ đồng bộ trong RAM lên đầu)."""
        sec = _normalize_section_filter(section)
        capped_limit = max(1, min(300, limit))

        pending_items: list[dict[str, Any]] = []
        for doc in reversed(self._pending_audits):
            if not sec or doc.get("section") == sec:
                item = _serialize_audit_doc(doc)
                item["pending"] = True
                pending_items.append(item)

        db_items: list[dict[str, Any]] = []
        if self.connected:
            try:
                query: dict[str, Any] = {"section": sec} if sec else {}
                raw_docs = await self.collection.find(query).sort("at", -1).limit(capped_limit).to_list(capped_limit)
                db_items = [_serialize_audit_doc(doc) for doc in raw_docs]
            except Exception as exc:
                log.warning("Không đọc được audit_logs từ MongoDB: %s", exc)

        return (pending_items + db_items)[:capped_limit]
