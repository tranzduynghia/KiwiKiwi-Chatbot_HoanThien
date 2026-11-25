"""Công cụ điều phối bất đồng bộ: semaphore động và khóa theo từng người dùng."""
from __future__ import annotations

import asyncio
import weakref
from typing import Callable


class DynamicSemaphore:
    """Semaphore bất đồng bộ cho phép thay đổi giới hạn request song song theo thời gian thực."""

    def __init__(self, get_limit: Callable[[], int]):
        self._get_limit = get_limit
        self._active = 0
        self._cond = asyncio.Condition()

    async def __aenter__(self) -> "DynamicSemaphore":
        async with self._cond:
            while self._active >= max(1, int(self._get_limit())):
                await self._cond.wait()
            self._active += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def notify_limit_changed(self) -> None:
        """Đánh thức các coroutine đang chờ khi giới hạn song song thay đổi trên Dashboard."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._notify())
        except RuntimeError:
            pass

    async def _notify(self) -> None:
        async with self._cond:
            self._cond.notify_all()


class UserLockManager:
    """Quản lý khóa asyncio.Lock riêng cho từng Discord User ID và tự giải phóng khi hết tham chiếu."""

    def __init__(self):
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def get(self, user_id: int) -> asyncio.Lock:
        """Lấy hoặc tạo mới khóa bất đồng bộ cho một người dùng cụ thể."""
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock
