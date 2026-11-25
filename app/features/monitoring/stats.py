"""Thống kê số lượng yêu cầu, tỉ lệ thành công, tính năng và model dùng theo ngày giờ Pacific."""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from typing import Any, Optional

from app.core.settings import PACIFIC_TZ


class UsageStats:
    """Bộ đếm thống kê sử dụng trong bộ nhớ kể từ thời điểm bot khởi động."""

    def __init__(self):
        self.total: int = 0
        self.ok: int = 0
        self.errors: int = 0
        self.features: Counter = Counter()
        self.daily_day: str = ""
        self.daily_models: Counter = Counter()
        self.last_error: Optional[tuple[float, str]] = None

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
