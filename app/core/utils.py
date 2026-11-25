"""Các hàm tiện ích thuần dùng chung cho gộp cấu hình, chia tin nhắn Discord và định dạng hiển thị."""
from __future__ import annotations

import copy
import re
from typing import Any


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Gộp sâu hai từ điển cấu hình (riêng khóa `overrides` được thay thế trọn gói để cho phép xóa mục)."""
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key != "overrides":
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def chunk_text(text: str, limit: int = 1900) -> list[str]:
    """Chia văn bản dài thành nhiều đoạn phù hợp giới hạn Discord, tự đóng/mở lại khối code ```."""
    chunks: list[str] = []
    reopen_code_block = False
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit * 0.5:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit * 0.5:
            cut = limit
        piece, remaining = remaining[:cut], remaining[cut:].lstrip("\n")
        if reopen_code_block:
            piece = "```\n" + piece
        reopen_code_block = piece.count("```") % 2 == 1
        if reopen_code_block:
            piece += "\n```"
        chunks.append(piece)
    if remaining.strip():
        chunks.append(("```\n" + remaining) if reopen_code_block else remaining)
    return chunks


def clean_title(title: str) -> str:
    """Làm sạch tiêu đề liên kết nguồn để không phá vỡ cú pháp Markdown của Discord."""
    return re.sub(r"[\[\]\n]", "", title)[:40]


def format_bytes(num_bytes: float) -> str:
    """Định dạng số byte sang chuỗi MB làm tròn."""
    return f"{num_bytes / 1024 / 1024:.0f} MB"


def format_duration(seconds: float) -> str:
    """Định dạng thời lượng giây sang chuỗi dễ đọc (ngày/giờ/phút/giây)."""
    total_sec = int(seconds)
    days, total_sec = divmod(total_sec, 86400)
    hours, total_sec = divmod(total_sec, 3600)
    minutes, secs = divmod(total_sec, 60)
    parts = (
        f"{days}d" if days else "",
        f"{hours}h" if hours else "",
        f"{minutes}m" if minutes else "",
        f"{secs}s",
    )
    return " ".join(part for part in parts if part)


def render_progress_bar(percent: float, width: int = 10) -> str:
    """Vẽ thanh tiến trình ký tự Unicode cho lệnh /status."""
    clamped = max(0.0, min(100.0, percent))
    filled = round(width * clamped / 100)
    return "█" * filled + "░" * (width - filled) + f" {clamped:.0f}%"
