"""Kiểm thử chuyển đổi lịch sử hội thoại và vòng đời lưu/lưu trữ phiên trên SessionRepository."""
from __future__ import annotations

import pytest

from app.features.sessions.history import clip_stored_text, history_to_contents


def test_history_to_contents_respects_char_limit_and_user_start():
    """Đảm bảo lịch sử cắt theo ngân sách ký tự và luôn bắt đầu bằng vai trò `user`."""
    doc = {
        "messages": [
            {"role": "model", "text": "tin nhắn lạc đầu"},
            {"role": "user", "text": "câu hỏi 1"},
            {"role": "model", "text": "trả lời 1"},
            {"role": "user", "text": "câu hỏi 2"},
            {"role": "model", "text": "trả lời 2"},
        ]
    }
    contents = history_to_contents(doc, max_history_messages=10, max_history_chars=1000)
    assert [c.role for c in contents] == ["user", "model", "user", "model"]
    assert contents[0].parts[0].text == "câu hỏi 1"

    # Giới hạn ký tự chỉ đủ giữ cặp cuối cùng
    short_contents = history_to_contents(doc, max_history_messages=10, max_history_chars=20)
    assert [c.role for c in short_contents] == ["user", "model"]
    assert short_contents[0].parts[0].text == "câu hỏi 2"


def test_clip_stored_text():
    """Kiểm tra cắt ngắn văn bản lưu phiên kèm dấu ba chấm."""
    assert clip_stored_text("abcdef", 10) == "abcdef"
    assert clip_stored_text("abcdef", 3) == "abc…"


@pytest.mark.asyncio
async def test_session_repository_save_load_and_end(app_container, fake_db):
    """Kiểm tra lưu lượt chat vào `sessions` + `archives`, nạp lại và đóng phiên qua `/refresh`."""
    await app_container.session_repo.attach_db(fake_db)
    uid = 555000111

    turns_1 = await app_container.session_repo.save_turn(uid, "Chào bot", "Chào bạn!", None, "gemini-2.5-flash")
    assert turns_1 == 1

    loaded = await app_container.session_repo.load(uid)
    assert loaded is not None
    assert loaded["turns"] == 1
    assert len(loaded["messages"]) == 2

    turns_2 = await app_container.session_repo.save_turn(uid, "Hôm nay thế nào?", "Rất tốt!", loaded, "gemini-2.5-flash")
    assert turns_2 == 2
    assert await app_container.session_repo.active_count() == 1
    assert await app_container.session_repo.archive_count() == 1

    ended = await app_container.session_repo.end_session(uid, "refresh")
    assert ended is not None
    assert ended["turns"] == 2
    assert await app_container.session_repo.load(uid) is None


@pytest.mark.asyncio
async def test_session_repository_search_messages(app_container, fake_db):
    """Kiểm tra tìm kiếm nội dung phiên chat theo từ khóa, vai trò và mã phiên trên MongoDB."""
    await app_container.session_repo.attach_db(fake_db)
    await app_container.session_repo.save_turn(1001, "Hướng dẫn cài đặt Python", "Bạn hãy tải Python 3.12.", None, "gemini-2.5-flash")
    await app_container.session_repo.save_turn(2002, "Thời tiết hôm nay", "Trời nắng đẹp.", None, "gemini-3.5-flash")

    # Tìm theo từ khóa không phân biệt hoa thường
    python_hits = await app_container.session_repo.search_messages(query="python")
    assert len(python_hits) == 2
    assert all(hit["chat_id"] and hit["timestamp"] for hit in python_hits)

    # Lọc thêm theo vai trò model và user_id
    model_only = await app_container.session_repo.search_messages(query="python", role="model", user_id="1001")
    assert len(model_only) == 1
    assert model_only[0]["role"] == "model"
    assert "Python 3.12" in model_only[0]["content"]

