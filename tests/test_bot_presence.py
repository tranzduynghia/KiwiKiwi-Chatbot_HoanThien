"""Kiểm thử đơn vị cho bộ dựng và áp dụng đồng thời Custom Status + Primary Activity trên Discord."""
from __future__ import annotations

import discord
import pytest

from app.bot.presence import apply_bot_presence, build_discord_presence, build_presence_gateway_payload
from app.features.config.validators import sanitize_presence_config


def test_build_discord_presence_dual_activities():
    """Khi cấu hình có cả custom_status và primary activity -> dựng mảng gồm cả 2 phần tử."""
    status, activities = build_discord_presence(
        {
            "status": "idle",
            "custom_status": "Ngôi nhà nhỏ của KiwiKiwi~",
            "activity_type": "listening",
            "activity_text": "m!help",
        }
    )
    assert status is discord.Status.idle
    assert len(activities) == 2
    assert isinstance(activities[0], discord.CustomActivity)
    assert activities[0].state == "Ngôi nhà nhỏ của KiwiKiwi~"
    assert isinstance(activities[1], discord.Activity)
    assert activities[1].type is discord.ActivityType.listening
    assert activities[1].name == "m!help"

    payload = build_presence_gateway_payload(status, activities)
    assert payload["op"] == 3
    assert payload["d"]["status"] == "idle"
    assert payload["d"]["since"] > 0
    assert len(payload["d"]["activities"]) == 2
    assert payload["d"]["activities"][0]["type"] == 4
    assert payload["d"]["activities"][0]["state"] == "Ngôi nhà nhỏ của KiwiKiwi~"
    assert payload["d"]["activities"][1]["type"] == 2
    assert payload["d"]["activities"][1]["name"] == "m!help"


def test_build_discord_presence_custom_only_and_legacy_migration():
    """Hỗ trợ chỉ đặt bong bóng Custom Status hoặc tự chuyển đổi dữ liệu legacy activity_type='custom'."""
    status, activities = build_discord_presence(
        {
            "status": "invisible",
            "custom_status": "Đang nghỉ ngơi",
            "activity_type": "none",
            "activity_text": "m!help",
        }
    )
    assert status is discord.Status.invisible
    assert len(activities) == 1
    assert isinstance(activities[0], discord.CustomActivity)
    assert activities[0].state == "Đang nghỉ ngơi"

    payload = build_presence_gateway_payload(status, activities)
    assert payload["d"]["status"] == "invisible"

    # Kiểm tra tương thích ngược tài liệu cũ lưu activity_type="custom"
    _, legacy_activities = build_discord_presence(
        {
            "status": "online",
            "activity_type": "custom",
            "activity_text": "Lời chào cũ",
        }
    )
    assert len(legacy_activities) == 1
    assert isinstance(legacy_activities[0], discord.CustomActivity)
    assert legacy_activities[0].state == "Lời chào cũ"

    sanitized = sanitize_presence_config("online", "custom", "Lời chào cũ")
    assert sanitized == {
        "status": "online",
        "custom_status": "Lời chào cũ",
        "activity_type": "none",
        "activity_text": "",
    }


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
        },
    )
    assert applied is True
    assert len(fake_bot.presence_updates) == 1
    applied_status, applied_activity = fake_bot.presence_updates[-1]
    assert applied_status is discord.Status.online
    assert applied_activity is None
