"""Kho lưu trữ phân quyền Discord (`discord_permissions`) kèm đệm RAM và hàng đợi đồng bộ Atlas."""
from __future__ import annotations

import copy
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("gemini-bot.permissions")


class PermissionRepository:
    """Quản lý đọc/ghi phân quyền Discord User ID trên RAM cache và MongoDB Atlas."""

    sync_key = "perms"

    def __init__(self, cache_ttl: float = 5.0):
        self.collection: Any = None
        self.cache_ttl = cache_ttl
        self._cache: dict[str, dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        # Bản đồ chờ đồng bộ: user_id -> tài liệu (upsert) hoặc None (delete)
        self._pending: dict[str, Optional[dict[str, Any]]] = {}
        self._seed_from_env()

    def _seed_from_env(self) -> None:
        """Khởi tạo danh sách quyền ban đầu từ biến môi trường `.env` khi chưa kết nối Atlas."""
        raw_admins = os.getenv("INITIAL_ADMIN_DISCORD_IDS", "")
        raw_status = os.getenv("STATUS_ALLOWED_IDS", "1240503090185109536,1568530089035956284")
        admin_ids = {item.strip() for item in re.split(r"[,\s]+", raw_admins) if item.strip().isdigit()}
        status_ids = {item.strip() for item in re.split(r"[,\s]+", raw_status) if item.strip().isdigit()}
        if not admin_ids:
            admin_ids = set(status_ids)
        now = datetime.now(timezone.utc).isoformat()
        for uid in sorted(admin_ids | status_ids):
            is_admin = uid in admin_ids
            self._cache[uid] = {
                "_id": uid,
                "user_id": uid,
                "label": "Admin khởi tạo (.env)" if is_admin else "Status (.env)",
                "is_admin": is_admin,
                "can_dashboard": is_admin,
                "can_status": is_admin or (uid in status_ids),
                "updated_at": now,
            }

    @property
    def connected(self) -> bool:
        """Cho biết collection `discord_permissions` đã kết nối hay chưa."""
        return self.collection is not None

    @property
    def pending_count(self) -> int:
        """Số bản ghi phân quyền đang chờ xả lên MongoDB Atlas."""
        return len(self._pending)

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `discord_permissions`, đồng bộ dữ liệu khởi tạo nếu collection còn trống."""
        self.collection = db["discord_permissions"]
        try:
            count = await self.collection.count_documents({})
            if count == 0 and self._cache:
                for doc in self._cache.values():
                    await self.collection.replace_one({"_id": doc["_id"]}, doc, upsert=True)
                log.info("Đã đồng bộ %d Discord User ID khởi tạo vào `discord_permissions`.", len(self._cache))
            await self.refresh(force=True)
        except Exception as exc:
            log.warning("Lỗi khởi tạo dữ liệu discord_permissions trên MongoDB: %s", exc)

    async def flush_pending(self) -> int:
        """Đẩy toàn bộ thao tác thêm/sửa/xóa quyền trong RAM lên MongoDB Atlas rồi giải phóng hàng đợi."""
        if not self.connected or not self._pending:
            return 0
        flushed = 0
        for uid in list(self._pending.keys()):
            value = self._pending[uid]
            if value is None:
                await self.collection.delete_one({"_id": uid})
            else:
                await self.collection.replace_one({"_id": uid}, copy.deepcopy(value), upsert=True)
            self._pending.pop(uid, None)
            flushed += 1
        return flushed

    async def refresh(self, force: bool = False) -> dict[str, dict[str, Any]]:
        """Nạp lại bảng phân quyền từ MongoDB Atlas nhưng vẫn giữ các thay đổi chờ đồng bộ trong RAM."""
        now = time.monotonic()
        if not self.connected:
            return copy.deepcopy(self._cache)
        if not force and (now - self._loaded_at) < self.cache_ttl:
            return copy.deepcopy(self._cache)
        try:
            docs = await self.collection.find({}).to_list(500)
            merged = {str(doc["_id"]): doc for doc in docs}
            for uid, pending_val in self._pending.items():
                if pending_val is None:
                    merged.pop(uid, None)
                else:
                    merged[uid] = copy.deepcopy(pending_val)
            self._cache = merged
            self._loaded_at = now
        except Exception as exc:
            log.warning("Không đọc được discord_permissions từ MongoDB (dùng cache): %s", exc)
        return copy.deepcopy(self._cache)

    def get_cached(self, user_id: str) -> Optional[dict[str, Any]]:
        """Đọc nhanh bản ghi quyền từ RAM cache phục vụ tính toán diff."""
        doc = self._cache.get(user_id)
        return copy.deepcopy(doc) if doc is not None else None

    def stage_upsert(self, doc: dict[str, Any]) -> None:
        """Cập nhật quyền ngay trên RAM và đưa vào hàng đợi đồng bộ Atlas."""
        uid = doc["user_id"]
        self._cache[uid] = copy.deepcopy(doc)
        self._loaded_at = time.monotonic()
        self._pending[uid] = copy.deepcopy(doc)

    def stage_delete(self, user_id: str) -> None:
        """Xóa quyền trên RAM và đánh dấu xóa trong hàng đợi đồng bộ Atlas."""
        self._cache.pop(user_id, None)
        self._loaded_at = time.monotonic()
        self._pending[user_id] = None
