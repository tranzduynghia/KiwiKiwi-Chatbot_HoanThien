"""
Bộ điều phối đồng bộ Write-Behind từ bộ đệm RAM lên MongoDB Atlas.
Mỗi tính năng tự sở hữu hàng đợi RAM và collection riêng; bộ điều phối này chịu trách nhiệm
gắn database, xả đồng bộ tuần tự dưới khóa chung và tổng hợp trạng thái đồng bộ cho Dashboard.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

log = logging.getLogger("gemini-bot.sync")


class SyncableRepository(Protocol):
    """Giao diện chuẩn cho các kho dữ liệu có cơ chế đệm RAM chờ đồng bộ lên MongoDB Atlas."""

    sync_key: str

    @property
    def pending_count(self) -> int:
        """Số lượng mục đang nằm trong hàng đợi RAM chờ ghi lên Atlas."""
        ...

    async def attach_db(self, db: Any) -> None:
        """Gắn đối tượng database MongoDB, tạo index và nạp dữ liệu ban đầu."""
        ...

    async def flush_pending(self) -> int:
        """Đẩy toàn bộ mục đang đệm trong RAM lên Atlas và giải phóng RAM; trả về số mục đã đẩy."""
        ...


class WriteBehindSyncCoordinator:
    """Điều phối gắn database và xả bộ đệm RAM của toàn bộ kho dữ liệu tính năng."""

    def __init__(self):
        self._repositories: list[SyncableRepository] = []
        self._lock = asyncio.Lock()
        self.db: Any = None
        self.last_flush_at: Optional[str] = None
        self.last_flush_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        """Cho biết bộ điều phối đã được gắn database MongoDB hay chưa."""
        return self.db is not None

    def register(self, repository: SyncableRepository) -> None:
        """Đăng ký một kho dữ liệu vào vòng đời đồng bộ write-behind."""
        self._repositories.append(repository)

    @property
    def pending_count(self) -> int:
        """Tổng số mục đang chờ đồng bộ trong RAM trên toàn bộ kho dữ liệu."""
        return sum(repo.pending_count for repo in self._repositories)

    def sync_status(self) -> dict[str, Any]:
        """Trả về ảnh chụp trạng thái đồng bộ giữ đúng định dạng JSON cho Dashboard."""
        by_key = {repo.sync_key: repo.pending_count for repo in self._repositories}
        return {
            "pending_count": self.pending_count,
            "pending_config": by_key.get("config", 0) > 0,
            "pending_perms": by_key.get("perms", 0),
            "pending_users": by_key.get("users", 0),
            "pending_audits": by_key.get("audits", 0),
            "last_flush_at": self.last_flush_at,
            "last_flush_error": self.last_flush_error,
        }

    async def attach_db(self, db: Any) -> None:
        """Gắn database cho mọi kho dữ liệu đã đăng ký rồi xả ngay hàng đợi RAM đang chờ."""
        self.db = db
        for repo in self._repositories:
            await repo.attach_db(db)
        await self.flush_pending()

    async def flush_pending(self) -> dict[str, Any]:
        """Đẩy toàn bộ dữ liệu đệm trong RAM lên MongoDB Atlas và giải phóng RAM ngay khi thành công."""
        if not self.connected or self.pending_count == 0:
            return self.sync_status()

        async with self._lock:
            flushed_total = 0
            try:
                for repo in self._repositories:
                    flushed_total += await repo.flush_pending()
                if flushed_total > 0:
                    self.last_flush_at = datetime.now(timezone.utc).isoformat()
                    self.last_flush_error = None
                    log.info(
                        "Đã đồng bộ %d mục từ bộ đệm RAM lên MongoDB Atlas và giải phóng RAM.",
                        flushed_total,
                    )
            except Exception as exc:
                self.last_flush_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning(
                    "Chưa đẩy được bộ đệm RAM lên MongoDB Atlas (còn %d mục, sẽ thử lại): %s",
                    self.pending_count,
                    self.last_flush_error,
                )

        return self.sync_status()
