"""Kho lưu trữ Audit Log kết hợp bộ đệm RAM chờ đồng bộ lên MongoDB collection `audit_logs`."""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

log = logging.getLogger("gemini-bot.audit")

MAX_PENDING_AUDITS = 500


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
        """Đẩy lần lượt từng bản ghi Audit Log trong RAM lên MongoDB và giải phóng ngay khỏi RAM."""
        if not self.connected or not self._pending_audits:
            return 0
        flushed = 0
        while self._pending_audits:
            audit_doc = copy.deepcopy(self._pending_audits[0])
            audit_doc.pop("pending", None)
            await self.collection.insert_one(audit_doc)
            self._pending_audits.pop(0)
            flushed += 1
        return flushed

    async def list_logs(self, limit: int = 100, section: Optional[str] = None) -> list[dict[str, Any]]:
        """Lấy danh sách Audit Log (ưu tiên bản ghi đang chờ đồng bộ trong RAM lên đầu)."""
        sec = (section or "").strip()
        capped_limit = max(1, min(300, limit))

        pending_items: list[dict[str, Any]] = []
        for doc in reversed(self._pending_audits):
            if not sec or doc.get("section") == sec:
                item = copy.deepcopy(doc)
                item["pending"] = True
                pending_items.append(item)

        db_items: list[dict[str, Any]] = []
        if self.connected:
            try:
                query: dict[str, Any] = {"section": sec} if sec else {}
                db_items = await self.collection.find(query, {"_id": 0}).sort("at", -1).to_list(capped_limit)
            except Exception as exc:
                log.warning("Không đọc được audit_logs từ MongoDB: %s", exc)

        return (pending_items + db_items)[:capped_limit]
