"""Kho lưu trữ cấu hình động (`bot_config`) kèm bộ đệm RAM và cơ chế Write-Behind."""
from __future__ import annotations

import copy
import fnmatch
import logging
import time
from typing import Any, Optional

from app.core.settings import build_default_config
from app.core.utils import deep_merge

log = logging.getLogger("gemini-bot.config")


class ConfigRepository:
    """Quản lý tài liệu cấu hình toàn cục `_id: "global"` trong RAM và MongoDB Atlas."""

    sync_key = "config"

    def __init__(self, cache_ttl: float = 5.0):
        self.collection: Any = None
        self.cache_ttl = cache_ttl
        self._config: dict[str, Any] = build_default_config()
        self._loaded_at: float = 0.0
        self._pending_config: Optional[dict[str, Any]] = None

    @property
    def connected(self) -> bool:
        """Cho biết collection `bot_config` đã được gắn MongoDB hay chưa."""
        return self.collection is not None

    @property
    def pending_count(self) -> int:
        """Trả về 1 nếu có bản cấu hình mới trong RAM chưa đồng bộ lên Atlas, ngược lại 0."""
        return 1 if self._pending_config is not None else 0

    @property
    def current(self) -> dict[str, Any]:
        """Truy cập nhanh cấu hình hiện tại trong RAM (không gọi mạng)."""
        return self._config

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `bot_config` và khởi tạo/nạp cấu hình mặc định nếu chưa có bản đệm mới."""
        self.collection = db["bot_config"]
        if self._pending_config is not None:
            return
        try:
            existing = await self.collection.find_one({"_id": "global"})
            if existing is None:
                default_doc = build_default_config()
                await self.collection.replace_one({"_id": "global"}, default_doc, upsert=True)
                self._config = default_doc
                log.info("Đã khởi tạo cấu hình mặc định trong MongoDB collection `bot_config`.")
            else:
                self._config = deep_merge(build_default_config(), existing)
            self._loaded_at = time.monotonic()
        except Exception as exc:
            log.warning("Lỗi khởi tạo dữ liệu bot_config trên MongoDB: %s", exc)

    async def flush_pending(self) -> int:
        """Đẩy bản cấu hình đang đệm trong RAM lên MongoDB Atlas rồi giải phóng bản đệm."""
        if not self.connected or self._pending_config is None:
            return 0
        doc = copy.deepcopy(self._pending_config)
        await self.collection.replace_one({"_id": "global"}, doc, upsert=True)
        self._pending_config = None
        return 1

    async def get_config(self, force: bool = False) -> dict[str, Any]:
        """Đọc cấu hình mới nhất (ưu tiên bản RAM nếu đang có thay đổi chờ đẩy lên Atlas)."""
        if self._pending_config is not None or not self.connected:
            return copy.deepcopy(self._config)
        now = time.monotonic()
        if not force and (now - self._loaded_at) < self.cache_ttl:
            return copy.deepcopy(self._config)
        try:
            doc = await self.collection.find_one({"_id": "global"})
            if doc:
                self._config = deep_merge(build_default_config(), doc)
                self._loaded_at = now
        except Exception as exc:
            log.warning("Không đọc được bot_config từ MongoDB (dùng cache): %s", exc)
        return copy.deepcopy(self._config)

    def stage_config(self, merged_doc: dict[str, Any]) -> None:
        """Áp dụng ngay cấu hình mới lên RAM và đưa vào hàng đợi chờ đồng bộ Atlas."""
        self._config = copy.deepcopy(merged_doc)
        self._loaded_at = time.monotonic()
        self._pending_config = copy.deepcopy(merged_doc)

    def ask_cfg(self) -> dict[str, Any]:
        """Trả về nhánh cấu hình lệnh `/ask` hiện hành."""
        return self._config.get("ask", {})

    def session_cfg(self) -> dict[str, Any]:
        """Trả về nhánh cấu hình phiên chat hiện hành."""
        return self._config.get("session", {})

    def models_cfg(self) -> dict[str, Any]:
        """Trả về nhánh cấu hình danh sách model Gemini hiện hành."""
        return self._config.get("models", {})

    def presence_cfg(self) -> dict[str, Any]:
        """Trả về nhánh cấu hình trạng thái hiển thị & hoạt động Discord của bot."""
        return self._config.get("presence", {})

    def model_override(self, model_id: str) -> dict[str, Any]:
        """Lấy cấu hình ghi đè (tên hiển thị, tag, ẩn/hiện) của một model cụ thể."""
        overrides = self.models_cfg().get("overrides") or {}
        override = overrides.get(model_id)
        return override if isinstance(override, dict) else {}

    def is_model_hidden(self, model_id: str) -> bool:
        """Kiểm tra model có đang bị ẩn theo override riêng hoặc mẫu wildcard `hidden_models` hay không."""
        mcfg = self.models_cfg()
        override = self.model_override(model_id)
        if override.get("hidden") is not None:
            return bool(override["hidden"])
        patterns = mcfg.get("hidden_models") or []
        return any(fnmatch.fnmatch(model_id.lower(), pattern.lower()) for pattern in patterns if pattern)
