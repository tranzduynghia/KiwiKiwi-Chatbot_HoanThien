"""Theo dõi thời gian tạm nghỉ (cooldown) của từng model Gemini khi gặp lỗi quota/quá tải."""
from __future__ import annotations

import asyncio
import re
import time

RETRYABLE_CODES = (404, 429, 500, 503)


class ModelCooldownTracker:
    """Quản lý trạng thái cooldown của các model để tự động né model đang lỗi trong chuỗi Auto."""

    def __init__(self):
        self._cooldowns: dict[str, float] = {}

    @staticmethod
    def is_retryable(exc: Exception) -> bool:
        """Kiểm tra lỗi có thể tự động chuyển sang model dự phòng khác hay không."""
        return isinstance(exc, asyncio.TimeoutError) or getattr(exc, "code", None) in RETRYABLE_CODES

    @staticmethod
    def failure_reason(exc: Exception) -> str:
        """Diễn giải ngắn gọn nguyên nhân thất bại của model bằng tiếng Việt."""
        if isinstance(exc, asyncio.TimeoutError):
            return "phản hồi quá lâu"
        code = getattr(exc, "code", None)
        return {
            429: "hết lượt/quota",
            500: "đang quá tải",
            503: "đang quá tải",
            404: "không còn tồn tại",
        }.get(code, f"lỗi {code}")

    @staticmethod
    def cooldown_seconds(exc: Exception) -> float:
        """Tính thời gian tạm nghỉ phù hợp theo loại lỗi (404, 429 limit/day/retry-in, timeout...)."""
        if isinstance(exc, asyncio.TimeoutError):
            return 60.0
        code = getattr(exc, "code", None)
        lowered = (getattr(exc, "message", "") or "").lower()
        if code == 404:
            return 6 * 3600.0
        if code == 429:
            if "limit: 0" in lowered:
                return 3600.0
            if "perday" in lowered or "per day" in lowered:
                return 1800.0
            match = re.search(r"retry in ([\d.]+)s", lowered)
            return min(float(match.group(1)) + 1, 600.0) if match else 60.0
        return 60.0

    def cooldown_left(self, model_id: str) -> float:
        """Trả về số giây cooldown còn lại của một model (0 nếu đã sẵn sàng)."""
        return max(0.0, self._cooldowns.get(model_id, 0.0) - time.monotonic())

    def mark_failure(self, model_id: str, exc: Exception) -> None:
        """Đánh dấu model vừa gặp lỗi tạm thời để chuỗi Auto bỏ qua trong thời gian cooldown."""
        if self.is_retryable(exc):
            self._cooldowns[model_id] = time.monotonic() + self.cooldown_seconds(exc)

    def clear_model(self, model_id: str) -> None:
        """Xóa cooldown ngay lập tức sau khi model phản hồi thành công."""
        self._cooldowns.pop(model_id, None)

    def clear_all(self) -> int:
        """Xóa toàn bộ cooldown đang lưu và trả về số model đã được giải phóng."""
        count = len(self._cooldowns)
        self._cooldowns.clear()
        return count
