"""Hàm phân tích metadata model Gemini (phiên bản, phân khúc, tính ổn định, nhãn hiển thị)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

NON_CHAT_KEYWORDS = (
    "embedding", "aqa", "tts", "live", "audio", "imagen", "veo", "lyria",
    "robotics", "computer-use", "gemma", "learnlm", "deep-research", "image",
)


@dataclass
class ModelInfo:
    """Thông tin rút gọn của một model chat dùng cho danh sách gợi ý trên Discord."""

    id: str
    display: str
    badge: str


def version_of(model_id: str) -> float:
    """Trích xuất số phiên bản từ tên model (ví dụ `gemini-2.5-flash` -> `2.5`)."""
    match = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id.lower())
    return float(match.group(1)) if match else 0.0


def tier_of(model_id: str) -> Optional[str]:
    """Phân loại phân khúc model (`lite`, `flash`, `pro` hoặc None)."""
    lowered = model_id.lower()
    if not lowered.startswith("gemini-"):
        return None
    if "lite" in lowered:
        return "lite"
    if "flash" in lowered:
        return "flash"
    if "pro" in lowered:
        return "pro"
    return None


def is_stable(model_id: str) -> bool:
    """Kiểm tra model có phải bản ổn định (không chứa `preview` hoặc `exp`) hay không."""
    lowered = model_id.lower()
    return "preview" not in lowered and "exp" not in lowered


def default_badge_for(model_id: str) -> str:
    """Gợi ý nhãn mặc định (heuristic) khi chưa cấu hình tag riêng trên Dashboard."""
    lowered = model_id.lower()
    if "pro" in lowered and version_of(lowered) >= 3:
        return "💳"
    if "preview" in lowered or "exp" in lowered:
        return "🧪"
    return "🆓"


def resolve_badge(model_id: str, override: dict[str, Any]) -> str:
    """Ưu tiên tag/is_free cấu hình trên MongoDB Dashboard, fallback sang heuristic mặc định."""
    if override.get("tag"):
        return str(override["tag"])
    if override.get("is_free") is True:
        return "🆓"
    if override.get("is_free") is False:
        return "💳"
    return default_badge_for(model_id)


def resolve_display_name(model_id: str, override: dict[str, Any], api_display: str = "") -> str:
    """Ưu tiên tên hiển thị tùy chỉnh trên Dashboard, sau đó tới tên API hoặc mã model."""
    if override.get("display_name"):
        return str(override["display_name"])
    return api_display or model_id
