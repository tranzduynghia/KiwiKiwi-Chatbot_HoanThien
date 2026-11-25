"""Kiểm thử phân tích phản hồi Gemini, giải thích lỗi và cooldown theo người dùng."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.errors import UserError
from app.features.chat.cooldown import UserCooldownTracker
from app.features.chat.parser import explain_error, parse_response


def test_parse_response_extracts_text_thoughts_sources_and_tokens():
    """Đảm bảo `parse_response` tách đúng nội dung chính, suy nghĩ, nguồn Google Search và token."""
    fake_resp = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="Đang phân tích bước 1...", thought=True),
                        SimpleNamespace(text="Xin chào ", thought=False),
                        SimpleNamespace(text="thế giới!", thought=False),
                    ]
                ),
                grounding_metadata=SimpleNamespace(
                    grounding_chunks=[
                        SimpleNamespace(web=SimpleNamespace(title="Tài liệu A", uri="https://a.example")),
                        SimpleNamespace(web=SimpleNamespace(title="Tài liệu A trùng", uri="https://a.example")),
                        SimpleNamespace(web=SimpleNamespace(title="Tài liệu B", uri="https://b.example")),
                    ],
                    web_search_queries=["kiwikiwi bot"],
                ),
                finish_reason=SimpleNamespace(name="STOP"),
            )
        ],
        prompt_feedback=None,
        usage_metadata=SimpleNamespace(total_token_count=321),
    )

    parsed = parse_response(fake_resp)
    assert parsed.text == "Xin chào thế giới!"
    assert parsed.thoughts == "Đang phân tích bước 1..."
    assert parsed.sources == [("Tài liệu A", "https://a.example"), ("Tài liệu B", "https://b.example")]
    assert parsed.queries == ["kiwikiwi bot"]
    assert parsed.finish == "STOP"
    assert parsed.blocked is None
    assert parsed.tokens == 321


def test_explain_error_messages():
    """Kiểm tra định dạng thông báo lỗi thân thiện cho UserError và TimeoutError."""
    assert explain_error(UserError("File quá lớn")) == "❌ File quá lớn"
    assert "phản hồi quá lâu" in explain_error(asyncio.TimeoutError())


def test_user_cooldown_tracker():
    """Đảm bảo người dùng phải chờ đủ thời gian cooldown giữa hai lần gọi `/ask`."""
    tracker = UserCooldownTracker()
    assert tracker.remaining_seconds(101, 5.0) <= 0
    tracker.mark_used(101)
    assert 0 < tracker.remaining_seconds(101, 5.0) <= 5.0
