"""Kiểm thử tầng hạ tầng cốt lõi: bảo mật scrypt, cookie signer, tiện ích chuỗi và DynamicSemaphore."""
from __future__ import annotations

import asyncio

import pytest

from app.core.concurrency import DynamicSemaphore, UserLockManager
from app.core.security import SessionTokenSigner, hash_password, verify_password
from app.core.utils import (
    chunk_text,
    clean_title,
    deep_merge,
    format_bytes,
    format_duration,
    render_progress_bar,
)


def test_password_hash_and_verify():
    """Đảm bảo băm scrypt tạo salt ngẫu nhiên và xác thực đúng/sai chính xác."""
    h1 = hash_password("MatKhauManh123!")
    h2 = hash_password("MatKhauManh123!")
    assert h1 != h2
    assert h1.startswith("scrypt$16384$8$1$")
    assert verify_password("MatKhauManh123!", h1) is True
    assert verify_password("SaiMatKhau", h1) is False
    assert verify_password("MatKhauManh123!", "invalid-hash") is False


def test_session_token_signer_roundtrip_and_tamper():
    """Kiểm tra ký và giải mã token cookie phiên, từ chối token bị sửa đổi."""
    signer = SessionTokenSigner("unit-test-secret")
    token = signer.encode({"username": "admin", "is_admin": True})
    decoded = signer.decode(token, max_age=300)
    assert decoded == {"username": "admin", "is_admin": True}
    assert signer.decode(token + "tampered", max_age=300) is None
    assert signer.decode(None, max_age=300) is None


def test_deep_merge_replaces_overrides_key():
    """Kiểm tra gộp sâu nhánh thường nhưng thay thế trọn gói nhánh `overrides` để cho phép xóa mục."""
    base = {
        "ask": {"user_cooldown": 5.0, "max_retries": 3},
        "models": {"hide_pro_models": True, "overrides": {"m1": {"tag": "🆓"}, "m2": {"hidden": True}}},
    }
    patch = {
        "ask": {"user_cooldown": 2.0},
        "models": {"overrides": {"m1": {"tag": "🧪"}}},
    }
    merged = deep_merge(base, patch)
    assert merged["ask"] == {"user_cooldown": 2.0, "max_retries": 3}
    assert merged["models"]["hide_pro_models"] is True
    assert merged["models"]["overrides"] == {"m1": {"tag": "🧪"}}


def test_chunk_text_preserves_code_fences():
    """Đảm bảo chia văn bản dài tự động đóng và mở lại khối code ``` giữa các đoạn."""
    body = "```python\n" + "\n".join(f"print({i})" for i in range(60)) + "\n```"
    chunks = chunk_text(body, limit=180)
    assert len(chunks) >= 2
    for piece in chunks:
        assert piece.count("```") % 2 == 0


def test_formatters_and_clean_title():
    """Kiểm tra các hàm định dạng dung lượng, thời gian, thanh tiến trình và tiêu đề nguồn."""
    assert clean_title("Tiêu đề [có] ngoặc\nxuống dòng") == "Tiêu đề có ngoặcxuống dòng"
    assert format_bytes(128 * 1024 * 1024) == "128 MB"
    assert format_duration(90061) == "1d 1h 1m 1s"
    assert render_progress_bar(50.0, width=10) == "█████░░░░░ 50%"


@pytest.mark.asyncio
async def test_dynamic_semaphore_and_user_locks():
    """Kiểm tra DynamicSemaphore cho phép thay đổi giới hạn động và UserLockManager trả về cùng khóa."""
    limit = 1
    sem = DynamicSemaphore(lambda: limit)
    entered = []

    async def worker(idx: int):
        async with sem:
            entered.append(idx)
            await asyncio.sleep(0.05)

    t1 = asyncio.create_task(worker(1))
    t2 = asyncio.create_task(worker(2))
    await asyncio.sleep(0.01)
    assert entered == [1]
    limit = 2
    sem.notify_limit_changed()
    await asyncio.gather(t1, t2)
    assert sorted(entered) == [1, 2]

    locks = UserLockManager()
    lock_a = locks.get(12345)
    lock_b = locks.get(12345)
    assert lock_a is lock_b
