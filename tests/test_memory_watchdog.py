"""Kiểm thử đơn vị và tích hợp cho MemoryWatchdog (vùng đệm 20MB, ngưỡng 464MB/400MB và 4 bước Sync-Before-Purge)."""
from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from app.core.memory_watchdog import (
    DEFAULT_CEILING_MB,
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_RECOVER_RSS_MB,
    DEFAULT_RESERVE_BYTES,
    DEFAULT_SYNC_TIMEOUT_SECONDS,
    DEFAULT_TRIGGER_RSS_MB,
    MemoryWatchdog,
)


def test_memory_watchdog_defaults_and_20mb_reserve_allocation():
    """Xác minh cấu hình mặc định đúng theo trần 488MB Northflank và vùng đệm 20MB sẵn sàng khi khởi động."""
    assert DEFAULT_CEILING_MB == 488.0
    assert DEFAULT_RESERVE_BYTES == 20 * 1024 * 1024
    assert DEFAULT_TRIGGER_RSS_MB == 464.0
    assert DEFAULT_RECOVER_RSS_MB == 400.0
    assert DEFAULT_CHECK_INTERVAL_SECONDS == 1.5
    assert DEFAULT_SYNC_TIMEOUT_SECONDS == 5.0

    wd = MemoryWatchdog(reserve_bytes=DEFAULT_RESERVE_BYTES)
    try:
        assert wd.reserve_ready is True
        snap = wd.status_snapshot()
        assert snap["reserve_mb"] == 20.0
        assert snap["reserve_ready"] is True
        assert snap["state"] == "ready"
        assert snap["status_label"] == "Dự phòng 20MB: Sẵn sàng"
        assert snap["check_interval_ms"] == 1500
    finally:
        wd.release_reserve()


@pytest.mark.asyncio
async def test_memory_watchdog_strict_four_step_sync_before_purge_and_recovery():
    """Xác minh chặt thứ tự 4 bước: Giải phóng đệm 20MB -> Đồng bộ MongoDB -> Dọn cache -> Phục hồi khi < 400MB."""
    rss_state = {"mb": 470.0}
    call_order: list[str] = []
    recorded_events: list[dict] = []

    wd: MemoryWatchdog

    async def fake_sync():
        # Bước 1 PHẢI đã giải phóng vùng đệm trước khi Bước 2 bắt đầu
        assert wd.reserve_ready is False
        call_order.append("sync_before_purge")
        return {"sessions": 3, "metrics": 1, "audits": 2}

    def fake_purge():
        # Bước 2 PHẢI chạy xong trước khi Bước 3 dọn cache
        assert "sync_before_purge" in call_order
        call_order.append("purge_caches")
        # Giả lập RSS giảm xuống 420MB (chưa dưới 400MB nên phải đợi nhịp kiểm tra sau)
        rss_state["mb"] = 420.0
        return {"session_cache": 5, "discord_user_cache": 4}

    async def fake_audit(event):
        call_order.append("record_audit_event")
        recorded_events.append(event)

    wd = MemoryWatchdog(
        reserve_bytes=1024 * 1024,  # 1MB để test nhanh nhưng vẫn dùng bytearray vật lý thật
        read_rss_bytes=lambda: int(rss_state["mb"] * 1024 * 1024),
        sync_before_purge=fake_sync,
        purge_caches=fake_purge,
        record_audit_event=fake_audit,
    )
    fake_bot = SimpleNamespace(_connection=SimpleNamespace(_messages=deque(["m1", "m2", "m3"])))
    wd.attach_bot(fake_bot)

    # Nhịp 1: RSS = 470MB > 464MB -> chạy Bước 1, 2, 3 nhưng dừng chờ phục hồi vì 420MB >= 400MB
    ev1 = await wd.check_once()
    assert ev1 is not None
    assert call_order == ["sync_before_purge", "purge_caches"]
    assert len(fake_bot._connection._messages) == 0
    assert wd.state == "released_waiting_recovery"
    assert wd.reserve_ready is False
    assert ev1["synced_total"] == 6
    assert ev1["purged_total"] == 12  # 5 + 4 + 3 discord messages
    assert len(recorded_events) == 0

    # Nhịp 2: RSS = 405MB (>= 400MB) -> vẫn tiếp tục chờ
    rss_state["mb"] = 405.0
    assert await wd.check_once() is None
    assert wd.reserve_ready is False

    # Nhịp 3: RSS = 385MB (< 400MB) -> Bước 4 cấp phát lại vùng đệm & ghi Audit Log
    rss_state["mb"] = 385.0
    ev2 = await wd.check_once()
    assert ev2 is not None
    assert wd.state == "ready"
    assert wd.reserve_ready is True
    assert call_order == ["sync_before_purge", "purge_caches", "record_audit_event"]
    assert len(recorded_events) == 1
    assert recorded_events[0]["synced_total"] == 6
    assert recorded_events[0]["recovered_rss_mb"] == 385.0
    assert recorded_events[0]["reserve_reallocated"] is True
    assert wd.status_snapshot()["emergency_count"] == 1


@pytest.mark.asyncio
async def test_memory_watchdog_sync_timeout_fallback_never_hangs():
    """Nếu MongoDB Atlas bị treo quá timeout -> watchdog vẫn chuyển sang Bước 3 dọn cache & GC và ghi nhận timeout."""
    rss_state = {"mb": 472.0}
    purged_called = False
    recorded_events: list[dict] = []

    async def hanging_sync():
        await asyncio.sleep(5.0)
        return {"sessions": 99}

    def fake_purge():
        nonlocal purged_called
        purged_called = True
        rss_state["mb"] = 350.0  # Giảm ngay dưới 400MB để kích hoạt Bước 4 tức thì
        return {"session_cache": 2}

    async def fake_audit(event):
        recorded_events.append(event)

    wd = MemoryWatchdog(
        reserve_bytes=256 * 1024,
        sync_timeout=0.05,
        read_rss_bytes=lambda: int(rss_state["mb"] * 1024 * 1024),
        sync_before_purge=hanging_sync,
        purge_caches=fake_purge,
        record_audit_event=fake_audit,
    )

    event = await wd.check_once()
    assert event is not None
    assert purged_called is True
    assert event["sync_timed_out"] is True
    assert wd.reserve_ready is True
    assert len(recorded_events) == 1
    assert recorded_events[0]["sync_timed_out"] is True


@pytest.mark.asyncio
async def test_container_memory_watchdog_end_to_end_sync_and_preserve_config(app_container, fake_db, fake_bot):
    """Kiểm thử tích hợp với AppContainer: đồng bộ bulk_write phiên chat, bot_metrics, audit_logs và bảo toàn bot_config."""
    app_container.usage_stats.attach_db(fake_db)
    await app_container.session_repo.attach_db(fake_db)
    await app_container.sync_coordinator.attach_db(fake_db)
    app_container.memory_watchdog.attach_bot(fake_bot)

    # 1) Tạo dữ liệu cache + dữ liệu bẩn chờ đồng bộ trong RAM
    await app_container.session_repo.save_turn(1001, "Tin nhắn đã lưu", "Phản hồi OK", None, "gemini-2.5-flash")
    assert len(app_container.session_repo._session_cache) == 1

    # Mô phỏng Atlas tạm lỗi khi lưu 1 lượt chat mới -> bị đẩy vào hàng đợi RAM _pending_turns
    fake_db["archives"].fail_writes = True
    await app_container.session_repo.save_turn(1002, "Tin nhắn lúc mạng chậm", "Phản hồi tạm", None, "gemini-2.5-flash")
    assert app_container.session_repo.pending_turn_count == 1
    fake_db["archives"].fail_writes = False

    # Ghi nhận metrics & 2 bản ghi audit chờ đồng bộ + cache user lookup + cooldown cũ
    app_container.usage_stats.record("gemini-2.5-flash", True, ["chat"])
    assert app_container.usage_stats.dirty is True
    app_container.audit_repo.enqueue({"at": "2026-11-25T00:00:00Z", "actor": "admin", "action": "t1", "section": "ask", "summary": "s1", "changes": []})
    app_container.audit_repo.enqueue({"at": "2026-11-25T00:00:01Z", "actor": "admin", "action": "t2", "section": "ask", "summary": "s2", "changes": []})
    app_container.user_lookup._user_cache["777888"] = (0.0, {"username": "cached_user"})
    app_container.user_cooldowns._last_used[555] = -99999.0
    app_container.model_cooldowns._cooldowns["old-model"] = 0.0

    # Lưu lại tham số bot config đang hoạt động để khẳng định KHÔNG bị xóa sau dọn RAM
    original_prompt = app_container.config_repo.current["system_prompt"]

    # 2) Giả lập RSS nhảy lên 470MB rồi rơi xuống 360MB sau khi giải phóng/dọn
    readings = iter([470.0, 450.0, 360.0])
    app_container.memory_watchdog._read_rss_bytes = lambda: int(next(readings, 360.0) * 1024 * 1024)

    event = await app_container.memory_watchdog.check_once()
    assert event is not None
    assert event["reserve_reallocated"] is True
    assert event["synced_details"].get("sessions") == 1
    assert event["synced_details"].get("metrics") == 1
    assert event["synced_details"].get("audits") == 2

    # Xác nhận dữ liệu đã được đồng bộ an toàn lên FakeDatabase
    assert app_container.session_repo.pending_turn_count == 0
    assert app_container.usage_stats.dirty is False
    assert await fake_db["bot_metrics"].find_one({"_id": "runtime_snapshot"}) is not None
    assert await fake_db["sessions"].find_one({"_id": 1002}) is not None

    # Xác nhận cache tạm đã bị xóa nhưng cấu hình bot vẫn còn nguyên vẹn
    assert len(app_container.session_repo._session_cache) == 0
    assert len(app_container.user_lookup._user_cache) == 0
    assert 555 not in app_container.user_cooldowns._last_used
    assert "old-model" not in app_container.model_cooldowns._cooldowns
    assert app_container.config_repo.current["system_prompt"] == original_prompt

    # Xác nhận Audit Log đã ghi nhận sự kiện giải cứu bộ nhớ dưới section="system"
    system_logs = await app_container.audit_service.list_logs(section="system")
    assert len(system_logs) == 1
    assert system_logs[0]["action"] == "memory_emergency_cleanup"
    assert "4 bản ghi" in system_logs[0]["summary"]
