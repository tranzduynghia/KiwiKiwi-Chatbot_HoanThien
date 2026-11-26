"""Quản lý vòng đời kết nối MongoDB Atlas dùng chung cho Bot, Dashboard và CLI."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import certifi
from pymongo import AsyncMongoClient

from app.core.security import redact_secrets

log = logging.getLogger("gemini-bot.database")


class MongoDatabaseManager:
    """Bọc AsyncMongoClient để quản lý kết nối, ping sức khỏe và thống kê dung lượng MongoDB Atlas."""

    def __init__(self, uri: str, db_name: str = "gemini_bot", timeout_ms: int = 8000):
        self.uri = (uri or "").strip()
        self.db_name = (db_name or "gemini_bot").strip()
        self.timeout_ms = timeout_ms
        self.client: Optional[AsyncMongoClient] = None
        self.db: Any = None
        self.ok: bool = False
        self.last_error: Optional[str] = None

    @property
    def enabled(self) -> bool:
        """Kiểm tra xem đã cấu hình chuỗi kết nối MONGODB_URI hay chưa."""
        return bool(self.uri)

    @property
    def connected(self) -> bool:
        """Kiểm tra xem đối tượng database đã được khởi tạo hay chưa."""
        return self.db is not None

    async def connect(self) -> Any:
        """Mở kết nối MongoDB Atlas (có cấu hình TLS CA cho URI mongodb+srv)."""
        if not self.enabled:
            log.warning("Chưa đặt MONGODB_URI → tắt bộ nhớ phiên & lưu cấu hình động trên MongoDB Atlas")
            return None
        kwargs: dict[str, Any] = {
            "serverSelectionTimeoutMS": self.timeout_ms,
            "tz_aware": True,
        }
        if self.uri.startswith("mongodb+srv://"):
            kwargs["tlsCAFile"] = certifi.where()

        # Đóng client cũ nếu đang kết nối lại để tránh rò rỉ socket
        if self.client is not None:
            await self.close()

        self.client = AsyncMongoClient(self.uri, **kwargs)
        self.db = self.client[self.db_name]
        return self.db

    async def close(self) -> None:
        """Đóng kết nối MongoDB Atlas an toàn khi tắt tiến trình."""
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None
        self.db = None

    async def ping(self) -> tuple[bool, float, str]:
        """Đo trạng thái và độ trễ (ms) kết nối tới MongoDB Atlas."""
        if not self.enabled or self.client is None:
            return False, 0.0, "tắt"
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self.client.admin.command("ping"), timeout=10)
            self.ok = True
            self.last_error = None
            return True, (time.perf_counter() - started) * 1000, "OK"
        except Exception as exc:
            self.ok = False
            self.last_error = redact_secrets(f"{type(exc).__name__}: {exc}", max_len=200)
            return False, (time.perf_counter() - started) * 1000, type(exc).__name__

    async def storage_mb(self) -> Optional[float]:
        """Đọc tổng dung lượng lưu trữ + index hiện tại (đơn vị MB) của database."""
        if self.client is None:
            return None
        try:
            stats = await asyncio.wait_for(self.client[self.db_name].command("dbStats"), timeout=10)
            return (stats.get("storageSize", 0) + stats.get("indexSize", 0)) / 1024 / 1024
        except Exception:
            return None
