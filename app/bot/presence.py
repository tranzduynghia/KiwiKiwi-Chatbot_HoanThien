"""Chuyển đổi cấu hình trạng thái từ MongoDB thành đối tượng Discord Status & Activity và áp dụng trực tiếp."""
from __future__ import annotations

import logging
from typing import Any, Optional

import discord

log = logging.getLogger("gemini-bot.presence")

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


def build_discord_presence(presence_cfg: dict[str, Any]) -> tuple[discord.Status, Optional[discord.BaseActivity]]:
    """Tạo cặp `(discord.Status, discord.BaseActivity | None)` từ từ điển cấu hình `presence`."""
    status_key = str(presence_cfg.get("status") or "online").strip().lower()
    activity_type = str(presence_cfg.get("activity_type") or "listening").strip().lower()
    activity_text = str(presence_cfg.get("activity_text") or "").strip()[:128]

    discord_status = STATUS_MAP.get(status_key, discord.Status.online)

    if not activity_text or activity_type == "none":
        return discord_status, None
    if activity_type == "custom":
        return discord_status, discord.CustomActivity(name=activity_text)

    mapped_type = ACTIVITY_TYPE_MAP.get(activity_type, discord.ActivityType.listening)
    return discord_status, discord.Activity(type=mapped_type, name=activity_text)


async def apply_bot_presence(bot: Any, presence_cfg: dict[str, Any]) -> bool:
    """Áp dụng trạng thái trực tuyến và hoạt động lên tiến trình bot Discord đang chạy."""
    if bot is None or not callable(getattr(bot, "change_presence", None)):
        return False
    is_ready_fn = getattr(bot, "is_ready", None)
    if callable(is_ready_fn) and not is_ready_fn():
        return False

    discord_status, activity = build_discord_presence(presence_cfg)
    try:
        await bot.change_presence(status=discord_status, activity=activity)
        log.info(
            "Đã áp dụng trạng thái Discord: status=%s, activity_type=%s, text=%r",
            presence_cfg.get("status"),
            presence_cfg.get("activity_type"),
            presence_cfg.get("activity_text"),
        )
        return True
    except Exception as exc:
        log.warning("Không thể cập nhật trạng thái Discord ngay lúc này: %s", exc)
        return False
