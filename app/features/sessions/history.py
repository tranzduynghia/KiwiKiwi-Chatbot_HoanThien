"""Tiện ích cắt gọn văn bản lưu ngữ cảnh và chuyển lịch sử phiên sang định dạng Content của Gemini."""
from __future__ import annotations

from typing import Optional

from google.genai import types


def clip_stored_text(text: str, max_chars: int) -> str:
    """Cắt ngắn nội dung tin nhắn trước khi lưu vào bộ đệm ngữ cảnh phiên."""
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def history_to_contents(
    doc: Optional[dict],
    *,
    max_history_messages: int = 20,
    max_history_chars: int = 24000,
) -> list[types.Content]:
    """Chuyển lịch sử tin nhắn của phiên thành danh sách `types.Content` tuân thủ giới hạn tin và ký tự."""
    if not doc:
        return []

    messages = (doc.get("messages") or [])[-max_history_messages:]
    kept: list[dict] = []
    total_chars = 0
    for message in reversed(messages):
        total_chars += len(message["text"])
        if total_chars > max_history_chars and kept:
            break
        kept.append(message)
    kept.reverse()

    # Gemini yêu cầu lịch sử hội thoại phải bắt đầu bằng lượt của người dùng (`user`)
    while kept and kept[0]["role"] != "user":
        kept.pop(0)

    return [types.Content(role=item["role"], parts=[types.Part(text=item["text"])]) for item in kept]
