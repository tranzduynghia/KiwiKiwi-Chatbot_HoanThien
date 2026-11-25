"""Thống kê số lượng yêu cầu, tỉ lệ thành công, tính năng và model dùng theo ngày giờ Pacific."""
from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.settings import PACIFIC_TZ

log = logging.getLogger("gemini-bot.stats")


class UsageStats:
    """Bộ đếm thống kê sử dụng trong bộ nhớ kèm khả năng đồng bộ nhanh lên MongoDB (`bot_metrics`)."""

    def __init__(self):
        self.total: int = 0
        self.ok: int = 0
        self.errors: int = 0
        self.features: Counter = Counter()
        self.daily_day: str = ""
        self.daily_models: Counter = Counter()
        self.last_error: Optional[tuple[float, str]] = None
        self._dirty: bool = False
        self.collection: Any = None
        self.bot_avatar_url: str = ""
        self.bot_name: str = "KiwiKiwi"

    @property
    def dirty(self) -> bool:
        """Cho biết bộ đếm thống kê đã thay đổi kể từ lần đồng bộ gần nhất hay chưa."""
        return self._dirty

    def attach_db(self, db: Any) -> None:
        """Gắn collection `bot_metrics` trên MongoDB Atlas để lưu ảnh chụp thống kê và thông tin nhận diện bot."""
        self.collection = db["bot_metrics"] if db is not None else None

    async def save_bot_branding(self, bot_avatar_url: str, bot_name: str) -> None:
        """Lưu `bot_avatar_url` và `bot_name` vào tài liệu `runtime_snapshot` trên `bot_metrics` khi bot sẵn sàng."""
        clean_url = str(bot_avatar_url or "").strip()
        clean_name = str(bot_name or "KiwiKiwi").strip() or "KiwiKiwi"
        if clean_url:
            self.bot_avatar_url = clean_url
        self.bot_name = clean_name
        if self.collection is None:
            return
        try:
            await self.collection.update_one(
                {"_id": "runtime_snapshot"},
                {
                    "$set": {
                        "bot_avatar_url": self.bot_avatar_url,
                        "bot_name": self.bot_name,
                        "branding_updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
                upsert=True,
            )
        except Exception as exc:
            log.warning("Không thể lưu bot_avatar_url vào bot_metrics: %s", exc)

    async def get_cached_bot_branding(self) -> tuple[str, str]:
        """Lấy `bot_avatar_url` và `bot_name` đã lưu từ RAM hoặc MongoDB `bot_metrics` (dùng khi bot đang khởi động)."""
        if self.bot_avatar_url:
            return self.bot_avatar_url, self.bot_name
        if self.collection is not None:
            try:
                doc = await self.collection.find_one({"_id": "runtime_snapshot"})
                if doc:
                    url = str(doc.get("bot_avatar_url") or "").strip()
                    name = str(doc.get("bot_name") or "").strip() or "KiwiKiwi"
                    if url:
                        self.bot_avatar_url = url
                    if name:
                        self.bot_name = name
            except Exception as exc:
                log.warning("Không đọc được bot_avatar_url từ bot_metrics: %s", exc)
        return self.bot_avatar_url, self.bot_name

    def roll_day(self) -> None:
        """Tự động đặt lại bộ đếm theo ngày khi sang ngày mới theo múi giờ Pacific (mốc reset quota Gemini)."""
        today = datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")
        if today != self.daily_day:
            self.daily_day = today
            self.daily_models = Counter()

    def record(self, model: str, ok: bool, features: list[str], err: Optional[str] = None) -> None:
        """Ghi nhận một lượt thực thi lệnh `/ask` thành công hoặc thất bại."""
        self.roll_day()
        self.total += 1
        self.daily_models[model] += 1
        self.features.update(features)
        if ok:
            self.ok += 1
        else:
            self.errors += 1
            self.last_error = (time.time(), err or "?")
        self._dirty = True

    def today_total(self) -> int:
        """Tổng số lượt gọi Gemini trong ngày hiện tại theo múi giờ Pacific."""
        self.roll_day()
        return sum(self.daily_models.values())

    def to_dict(self) -> dict[str, Any]:
        """Xuất dữ liệu thống kê dạng từ điển JSON cho Web Dashboard."""
        self.roll_day()
        return {
            "total": self.total,
            "ok": self.ok,
            "errors": self.errors,
            "success_rate": round(self.ok / self.total * 100, 1) if self.total else None,
            "today_total": self.today_total(),
            "daily_models": dict(self.daily_models.most_common(10)),
            "features": dict(self.features.most_common()),
            "last_error": (
                {"at": int(self.last_error[0]), "message": self.last_error[1]}
                if self.last_error
                else None
            ),
        }

    async def flush_pending(self) -> int:
        """Đẩy ảnh chụp bộ đếm thống kê hiện tại lên collection `bot_metrics` nếu có thay đổi chưa lưu."""
        if self.collection is None or not self._dirty:
            return 0
        payload = self.to_dict()
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()
        if self.bot_avatar_url:
            payload["bot_avatar_url"] = self.bot_avatar_url
        if self.bot_name:
            payload["bot_name"] = self.bot_name
        await self.collection.update_one(
            {"_id": "runtime_snapshot"},
            {"$set": payload},
            upsert=True,
        )
        self._dirty = False
        return 1
