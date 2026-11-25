"""Chuyển đổi cấu hình trạng thái từ MongoDB thành Discord Status + mảng Activities (Custom Status & Primary Activity) và áp dụng trực tiếp."""
from __future__ import annotations

import logging
import time
from typing import Any

import discord

log = logging.getLogger("gemini-bot.presence")

DISCORD_PRESENCE_OPCODE = 3

STATUS_MAP: dict[str, discord.Status] = {
    "online": discord.Status.online,
    "idle": discord.Status.idle,
    "dnd": discord.Status.dnd,
    "invisible": discord.Status.invisible,
}

ACTIVITY_TYPE_MAP: dict[str, discord.ActivityType] = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}


def build_discord_presence(presence_cfg: dict[str, Any]) -> tuple[discord.Status, list[discord.BaseActivity]]:
    """Tạo `(discord.Status, list[discord.BaseActivity])` chứa đồng thời Custom Status (nếu có) và hoạt động chính (nếu có)."""
    status_key = str(presence_cfg.get("status") or "online").strip().lower()
    custom_status = str(presence_cfg.get("custom_status") or "").strip()[:128]
    activity_type = str(presence_cfg.get("activity_type") or "none").strip().lower()
    activity_text = str(presence_cfg.get("activity_text") or "").strip()[:128]

    # Tương thích ngược với tài liệu cũ từng lưu activity_type="custom"
    if activity_type == "custom":
        if not custom_status and activity_text:
            custom_status = activity_text
        activity_type = "none"
        activity_text = ""

    discord_status = STATUS_MAP.get(status_key, discord.Status.online)
    activities: list[discord.BaseActivity] = []

    if custom_status:
        activities.append(discord.CustomActivity(name=custom_status))

    if activity_type != "none" and activity_text:
        mapped_type = ACTIVITY_TYPE_MAP.get(activity_type, discord.ActivityType.listening)
        activities.append(discord.Activity(type=mapped_type, name=activity_text))

    return discord_status, activities


def build_presence_gateway_payload(
    discord_status: discord.Status,
    activities: list[discord.BaseActivity],
) -> dict[str, Any]:
    """Tạo payload JSON Gateway OP 3 (Presence Update) gửi đồng thời nhiều hoạt động lên Discord."""
    status_str = "invisible" if discord_status is discord.Status.offline else str(discord_status)
    since = int(time.time() * 1000) if status_str == "idle" else 0
    return {
        "op": DISCORD_PRESENCE_OPCODE,
        "d": {
            "activities": [act.to_dict() for act in activities],
            "afk": False,
            "since": since,
            "status": status_str,
        },
    }


def _sync_local_guild_presence(bot: Any, discord_status: discord.Status, activities: list[discord.BaseActivity]) -> None:
    """Đồng bộ cache cục bộ `guild.me.activities` và `guild.me.status` khớp với trạng thái vừa gửi lên Gateway."""
    for guild in getattr(bot, "guilds", []) or []:
        me = getattr(guild, "me", None)
        if me is None:
            continue
        try:
            me.activities = tuple(activities)
            me.status = discord_status
        except Exception:
            pass


async def apply_bot_presence(bot: Any, presence_cfg: dict[str, Any]) -> bool:
    """Áp dụng trạng thái trực tuyến, bong bóng Custom Status và hoạt động chính lên tiến trình bot Discord."""
    if bot is None:
        return False
    is_ready_fn = getattr(bot, "is_ready", None)
    if callable(is_ready_fn) and not is_ready_fn():
        return False

    ws = getattr(bot, "ws", None)
    ws_send = getattr(ws, "send_as_json", None) if ws is not None else None
    change_presence_fn = getattr(bot, "change_presence", None)
    if not callable(ws_send) and not callable(change_presence_fn):
        return False

    discord_status, activities = build_discord_presence(presence_cfg)
    try:
        if len(activities) > 1 and callable(ws_send):
            # discord.py Client.change_presence chỉ nhận 1 activity đơn lẻ; gửi trực tiếp Gateway OP 3 để hiển thị song song cả 2
            payload = build_presence_gateway_payload(discord_status, activities)
            await ws_send(payload)
            _sync_local_guild_presence(bot, discord_status, activities)
        elif callable(change_presence_fn):
            single_activity = activities[0] if activities else None
            if len(activities) > 1:
                log.warning("Bot không hỗ trợ gửi trực tiếp Gateway đa hoạt động; tạm áp dụng hoạt động đầu tiên.")
            await change_presence_fn(status=discord_status, activity=single_activity)
        else:
            payload = build_presence_gateway_payload(discord_status, activities)
            await ws_send(payload)
            _sync_local_guild_presence(bot, discord_status, activities)

        log.info(
            "Đã áp dụng trạng thái Discord: status=%s, custom_status=%r, activity_type=%s, activity_text=%r (tổng %d hoạt động)",
            presence_cfg.get("status"),
            presence_cfg.get("custom_status"),
            presence_cfg.get("activity_type"),
            presence_cfg.get("activity_text"),
            len(activities),
        )
        return True
    except Exception as exc:
        log.warning("Không thể cập nhật trạng thái Discord ngay lúc này: %s", exc)
        return False
