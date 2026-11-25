"""Kiểm thử đơn vị cho chế độ Smart Merge mặc định và Auto-Rotate luân phiên mỗi 20s của Bot Presence."""
from __future__ import annotations

import asyncio

import discord
import pytest

from app.bot.presence import (
    PRESENCE_ROTATE_INTERVAL_SECONDS,
    apply_bot_presence,
    build_presence_plan,
    stop_presence_rotation,
)
from app.features.config.validators import sanitize_presence_config


def test_build_presence_plan_smart_merge_default():
    """Mặc định khi điền cả Custom Status và Primary Activity -> gộp Smart Merge thành 1 bong bóng chữ phân tách bằng ·."""
    status, steps, mode = build_presence_plan(
        {
            "status": "online",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "Songs",
            "auto_rotate": False,
        }
    )
    assert status is discord.Status.online
    assert mode == "merge"
    assert len(steps) == 1
    assert isinstance(steps[0], discord.CustomActivity)
    assert steps[0].state == "Ngôi nhà nhỏ của KiwiKiwi~ · Listening to Songs"


def test_build_presence_plan_auto_rotate_two_steps():
    """Khi bật auto_rotate=True và điền cả 2 -> dựng đúng 2 bước luân phiên (Custom Status -> Primary Activity)."""
    assert PRESENCE_ROTATE_INTERVAL_SECONDS == 20.0
    status, steps, mode = build_presence_plan(
        {
            "status": "idle",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "m!help",
            "auto_rotate": True,
        }
    )
    assert status is discord.Status.idle
    assert mode == "rotate"
    assert len(steps) == 2
    assert isinstance(steps[0], discord.CustomActivity)
    assert steps[0].state == "Ngôi nhà nhỏ của KiwiKiwi~"
    assert isinstance(steps[1], discord.Activity)
    assert steps[1].type is discord.ActivityType.listening
    assert steps[1].name == "m!help"


def test_build_presence_plan_single_and_legacy_migration():
    """Hỗ trợ chỉ đặt Custom Status, chỉ đặt Primary Activity, hoặc tự chuyển đổi dữ liệu legacy activity_type='custom'."""
    status, steps, mode = build_presence_plan(
        {
            "status": "invisible",
            "custom_status": "Đang nghỉ ngơi",
            "activity_type": "none",
            "activity_text": "m!help",
            "auto_rotate": True,
        }
    )
    assert status is discord.Status.invisible
    assert mode == "single"
    assert len(steps) == 1
    assert isinstance(steps[0], discord.CustomActivity)
    assert steps[0].state == "Đang nghỉ ngơi"

    _, legacy_steps, legacy_mode = build_presence_plan(
        {
            "status": "online",
            "activity_type": "custom",
            "activity_text": "Lời chào cũ",
        }
    )
    assert legacy_mode == "single"
    assert isinstance(legacy_steps[0], discord.CustomActivity)
    assert legacy_steps[0].state == "Lời chào cũ"

    sanitized = sanitize_presence_config("online", "custom", "Lời chào cũ", auto_rotate=True)
    assert sanitized == {
        "status": "online",
        "custom_status": "Lời chào cũ",
        "activity_type": "none",
        "activity_text": "",
        "auto_rotate": True,
    }


@pytest.mark.asyncio
async def test_apply_bot_presence_auto_rotate_alternates_and_stops(fake_bot):
    """Bật auto_rotate sẽ luân phiên gửi Custom Status và Primary Activity riêng biệt, và dừng sạch khi tắt."""
    applied = await apply_bot_presence(
        fake_bot,
        {
            "status": "online",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "m!help",
            "auto_rotate": True,
        },
        rotate_interval=0.02,
    )
    assert applied is True
    # Bước đầu áp dụng ngay CustomActivity
    assert len(fake_bot.presence_updates) == 1
    assert isinstance(fake_bot.presence_updates[0][1], discord.CustomActivity)
    assert fake_bot.presence_updates[0][1].state == "Ngôi nhà nhỏ của KiwiKiwi~"

    # Đợi vài chu kỳ luân phiên để xác nhận đổi sang Listening to m!help rồi quay lại CustomActivity
    await asyncio.sleep(0.065)
    assert len(fake_bot.presence_updates) >= 3
    assert isinstance(fake_bot.presence_updates[1][1], discord.Activity)
    assert fake_bot.presence_updates[1][1].type is discord.ActivityType.listening
    assert fake_bot.presence_updates[1][1].name == "m!help"
    assert isinstance(fake_bot.presence_updates[2][1], discord.CustomActivity)
    assert fake_bot.presence_updates[2][1].state == "Ngôi nhà nhỏ của KiwiKiwi~"

    # Chuyển về Smart Merge -> dừng tác vụ luân phiên cũ và gửi 1 bong bóng gộp duy nhất
    await apply_bot_presence(
        fake_bot,
        {
            "status": "online",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "m!help",
            "auto_rotate": False,
        },
    )
    assert getattr(fake_bot, "_presence_rotate_task", None) is None
    last_activity = fake_bot.presence_updates[-1][1]
    assert isinstance(last_activity, discord.CustomActivity)
    assert last_activity.state == "Ngôi nhà nhỏ của KiwiKiwi~ · Listening to m!help"
    stop_presence_rotation(fake_bot)


@pytest.mark.asyncio
async def test_apply_bot_presence_clears_activities_when_both_empty(fake_bot):
    """Khi cả Custom Status và Primary Activity đều trống -> gọi change_presence với activity=None."""
    applied = await apply_bot_presence(
        fake_bot,
        {
            "status": "online",
            "custom_status": "",
            "activity_type": "none",
            "activity_text": "",
            "auto_rotate": False,
        },
    )
    assert applied is True
    assert len(fake_bot.presence_updates) == 1
    applied_status, applied_activity = fake_bot.presence_updates[-1]
    assert applied_status is discord.Status.online
    assert applied_activity is None
