"""Phân tích phản hồi từ Gemini API và chuyển đổi ngoại lệ thành thông báo tiếng Việt thân thiện."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Optional

from google.genai import errors as genai_errors

from app.core.errors import UserError


@dataclass
class ParsedResponse:
    """Dữ liệu đã trích xuất từ phản hồi của Gemini (nội dung, suy nghĩ, nguồn web, token)."""

    text: str
    thoughts: str
    sources: list[tuple[str, str]]
    queries: list[str]
    finish: str
    blocked: Optional[str]
    tokens: Optional[int]


def parse_response(resp: Any) -> ParsedResponse:
    """Trích xuất văn bản trả lời, tóm tắt suy nghĩ, nguồn Google Search và siêu dữ liệu từ phản hồi Gemini."""
    texts: list[str] = []
    thoughts: list[str] = []
    sources: list[tuple[str, str]] = []
    queries: list[str] = []

    candidate = resp.candidates[0] if resp.candidates else None
    if candidate and candidate.content and candidate.content.parts:
        for part in candidate.content.parts:
            if part.text and part.thought:
                thoughts.append(part.text)
            elif part.text:
                texts.append(part.text)

    grounding = getattr(candidate, "grounding_metadata", None) if candidate else None
    if grounding:
        seen_uris: set[str] = set()
        for chunk in grounding.grounding_chunks or []:
            if chunk.web and chunk.web.uri and chunk.web.uri not in seen_uris:
                seen_uris.add(chunk.web.uri)
                sources.append((chunk.web.title or chunk.web.uri, chunk.web.uri))
        queries = list(grounding.web_search_queries or [])

    finish = candidate.finish_reason.name if candidate and candidate.finish_reason else "?"
    block_reason = None
    prompt_feedback = getattr(resp, "prompt_feedback", None)
    if prompt_feedback and prompt_feedback.block_reason:
        block_reason = prompt_feedback.block_reason.name
    usage = getattr(resp, "usage_metadata", None)

    return ParsedResponse(
        text="".join(texts).strip(),
        thoughts="\n".join(thoughts).strip(),
        sources=sources,
        queries=queries,
        finish=finish,
        blocked=block_reason,
        tokens=getattr(usage, "total_token_count", None),
    )


def explain_error(exc: Exception) -> str:
    """Chuyển đổi lỗi kỹ thuật sang thông báo tiếng Việt ngắn gọn hiển thị cho người dùng Discord."""
    if isinstance(exc, UserError):
        return f"❌ {exc}"
    if isinstance(exc, asyncio.TimeoutError):
        return "⌛ Gemini phản hồi quá lâu, hãy thử lại hoặc chọn model nhẹ hơn."
    if isinstance(exc, genai_errors.APIError):
        msg = exc.message or str(exc)
        if exc.code == 429:
            if "limit: 0" in msg:
                return (
                    "🚫 Model này **không có quota free** (limit: 0). "
                    "Hãy chọn model có nhãn 🆓 hoặc bật billing cho API key."
                )
            match = re.search(r"retry in ([\d.]+)s", msg, re.I)
            hint = f" Thử lại sau ~{float(match.group(1)):.0f}s." if match else ""
            return f"🚦 Hết giới hạn tốc độ/quota free của Gemini.{hint} Có thể đổi model khác."
        if exc.code in (401, 403):
            return "🔑 API key không hợp lệ/không có quyền dùng model này (hoặc khu vực bị chặn)."
        if exc.code == 404:
            return "❓ Model không tồn tại hoặc đã bị gỡ. Hãy chọn lại model từ gợi ý."
        if exc.code in (500, 503):
            return "🔥 Gemini đang quá tải, hãy thử lại sau ít phút."
        from app.core.security import redact_secrets

        return f"⚠️ Lỗi Gemini ({exc.code}): {redact_secrets(msg, max_len=300)}"
    return f"⚠️ Lỗi không xác định: {type(exc).__name__}"
