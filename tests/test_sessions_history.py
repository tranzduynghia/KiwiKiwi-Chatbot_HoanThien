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
    """Kiểm tra duyệt toàn bộ bản ghi và lọc theo từ khóa, username, vai trò server, user_id trên MongoDB."""
    await app_container.session_repo.attach_db(fake_db)
    await app_container.session_repo.save_turn(
        1001,
        "Hướng dẫn cài đặt Python",
        "Bạn hãy tải Python 3.12.",
        None,
        "gemini-2.5-flash",
        user_meta={
            "username": "alice_dev",
            "display_name": "Alice Dev",
            "avatar_url": "https://cdn.discordapp.com/avatars/1001/alice.png",
            "guild_name": "Kiwi Server",
            "server_roles": [{"id": "901", "name": "Admin", "color": "#e11d48"}],
        },
    )
    await app_container.session_repo.save_turn(
        2002,
        "Thời tiết hôm nay",
        "Trời nắng đẹp.",
        None,
        "gemini-3.5-flash",
        user_meta={
            "username": "bob_user",
            "display_name": "Bob",
            "avatar_url": "https://cdn.discordapp.com/avatars/2002/bob.png",
            "guild_name": "Kiwi Server",
            "server_roles": [{"id": "902", "name": "Member", "color": "#22c55e"}],
        },
    )

    # Thêm lượt thứ 2 vào cùng phiên của Alice -> vẫn phải gom thành đúng 1 dòng theo session_id
    alice_doc = await app_container.session_repo.load(1001)
    await app_container.session_repo.save_turn(
        1001,
        "Câu hỏi thứ 2 trong cùng phiên",
        "Trả lời lượt 2.",
        alice_doc,
        "gemini-2.5-flash",
    )

    # Duyệt toàn bộ: chỉ 2 dòng (mỗi phiên 1 dòng dù có 6 tin nhắn)
    all_res = await app_container.session_repo.search_messages()
    assert all_res["total"] == 2
    assert len(all_res["items"]) == 2

    # Lọc theo username -> 1 phiên duy nhất, preview là câu hỏi đầu tiên của người dùng
    alice_res = await app_container.session_repo.search_messages(username="alice")
    assert alice_res["total"] == 1
    assert alice_res["items"][0]["avatar_url"].endswith("alice.png")
    assert alice_res["items"][0]["preview"] == "Hướng dẫn cài đặt Python"
    assert alice_res["items"][0]["turns"] == 2
    assert alice_res["items"][0]["message_count"] == 4

    # Lọc theo vai trò server (ID hoặc tên vai trò)
    role_res = await app_container.session_repo.search_messages(server_role="902")
    assert role_res["total"] == 1
    assert role_res["items"][0]["username"] == "bob_user"

    # Lọc kết hợp từ khóa + vai trò model + user_id
    model_only = await app_container.session_repo.search_messages(query="python", role="model", user_id="1001")
    assert model_only["total"] == 1
    assert model_only["items"][0]["preview"] == "Hướng dẫn cài đặt Python"


