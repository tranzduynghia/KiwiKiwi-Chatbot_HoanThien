"""Theo dõi thời gian chờ (cooldown) giữa hai lần dùng lệnh `/ask` của từng người dùng."""
from __future__ import annotations

import time


class UserCooldownTracker:
    """Giới hạn tần suất gọi `/ask` theo Discord User ID và tự dọn mục cũ để tiết kiệm RAM."""

    def __init__(self, max_entries: int = 2000, stale_seconds: float = 3600.0):
        self._last_used: dict[int, float] = {}
        self._max_entries = max_entries
        self._stale_seconds = stale_seconds

    def remaining_seconds(self, user_id: int, cooldown_seconds: float) -> float:
        """Tính số giây người dùng còn phải chờ (trả về <= 0 nếu đã được phép hỏi)."""
        now = time.monotonic()
        return cooldown_seconds - (now - self._last_used.get(user_id, -1e9))

    def mark_used(self, user_id: int) -> None:
        """Ghi nhận thời điểm gọi `/ask` mới nhất và dọn các mục quá hạn khi bản đồ lớn."""
        now = time.monotonic()
        self._last_used[user_id] = now
        if len(self._last_used) > self._max_entries:
            stale_keys = [key for key, ts in self._last_used.items() if now - ts > self._stale_seconds]
            for key in stale_keys:
                self._last_used.pop(key, None)
