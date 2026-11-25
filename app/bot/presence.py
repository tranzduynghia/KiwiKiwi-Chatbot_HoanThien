"""Chuyển đổi cấu hình trạng thái từ MongoDB thành Discord Presence theo chế độ Smart Merge hoặc Luân phiên 20 giây."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import discord

log = logging.getLogger("gemini-bot.presence")

PRESENCE_ROTATE_INTERVAL_SECONDS = 20.0

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

ACTIVITY_PREFIX_LABELS: dict[str, str] = {
    "playing": "Playing",
    "listening": "Listening to",
    "watching": "Watching",
    "competing": "Competing in",
}


def format_smart_merged_status(custom_status: str, activity_type: str, activity_text: str) -> str:
    """Gộp Custom Status và hoạt động chính thành 1 dòng bong bóng chữ phân tách bằng dấu `·` (tối đa 128 ký tự)."""
    prefix = ACTIVITY_PREFIX_LABELS.get(activity_type, "Listening to")
    return f"{custom_status} · {prefix} {activity_text}"[:128]


def build_presence_plan(
    presence_cfg: dict[str, Any],
) -> tuple[discord.Status, list[Optional[discord.BaseActivity]], str]:
    """Dựng kế hoạch hiển thị Presence: `(status, danh_sach_activity, che_do)` (`merge` | `rotate` | `single` | `none`).

    Vì giao diện Discord ẩn hoạt động chuẩn (Playing/Listening) nếu gửi chung mảng với Custom Status (type 4):
    - Khi điền cả 2 và `auto_rotate=False` (mặc định): Smart Merge gộp thành 1 CustomActivity duy nhất.
    - Khi điền cả 2 và `auto_rotate=True`: Luân phiên gửi từng hoạt động riêng mỗi 20 giây.
    """
    status_key = str(presence_cfg.get("status") or "online").strip().lower()
    custom_status = str(presence_cfg.get("custom_status") or "").strip()[:128]
    activity_type = str(presence_cfg.get("activity_type") or "none").strip().lower()
    activity_text = str(presence_cfg.get("activity_text") or "").strip()[:128]
    auto_rotate = bool(presence_cfg.get("auto_rotate", False))

    # Tương thích ngược với tài liệu cũ từng lưu activity_type="custom"
    if activity_type == "custom":
        if not custom_status and activity_text:
            custom_status = activity_text
        activity_type = "none"
        activity_text = ""

    discord_status = STATUS_MAP.get(status_key, discord.Status.online)
    has_custom = bool(custom_status)
    has_primary = activity_type != "none" and bool(activity_text)

    if has_custom and has_primary:
        if auto_rotate:
            mapped_type = ACTIVITY_TYPE_MAP.get(activity_type, discord.ActivityType.listening)
            return (
                discord_status,
                [
                    discord.CustomActivity(name=custom_status),
                    discord.Activity(type=mapped_type, name=activity_text),
                ],
                "rotate",
            )
        merged_text = format_smart_merged_status(custom_status, activity_type, activity_text)
        return discord_status, [discord.CustomActivity(name=merged_text)], "merge"

    if has_custom:
        return discord_status, [discord.CustomActivity(name=custom_status)], "single"

    if has_primary:
        mapped_type = ACTIVITY_TYPE_MAP.get(activity_type, discord.ActivityType.listening)
        return discord_status, [discord.Activity(type=mapped_type, name=activity_text)], "single"

    return discord_status, [None], "none"


def stop_presence_rotation(bot: Any) -> None:
    """Hủy tác vụ nền luân phiên trạng thái Discord nếu đang chạy trên bot."""
    if bot is None:
        return
    task = getattr(bot, "_presence_rotate_task", None)
    if task is not None and not task.done():
        task.cancel()
    try:
        setattr(bot, "_presence_rotate_task", None)
    except Exception:
        pass


async def _presence_rotation_loop(
    bot: Any,
    discord_status: discord.Status,
    steps: list[Optional[discord.BaseActivity]],
    interval_seconds: float,
) -> None:
    """Vòng lặp nền luân phiên giữa Bong bóng Custom Status và Hoạt động chính sau mỗi `interval_seconds` giây."""
    idx = 1
    delay = max(0.01, float(interval_seconds))
    try:
        while True:
            await asyncio.sleep(delay)
            is_closed_fn = getattr(bot, "is_closed", None)
            if callable(is_closed_fn) and is_closed_fn():
                break
            is_ready_fn = getattr(bot, "is_ready", None)
            if callable(is_ready_fn) and not is_ready_fn():
                continue
            try:
                await bot.change_presence(status=discord_status, activity=steps[idx % len(steps)])
            except Exception as exc:
                log.warning("Lỗi khi luân phiên trạng thái Discord: %s", exc)
            idx += 1
    except asyncio.CancelledError:
        return


async def apply_bot_presence(
    bot: Any,
    presence_cfg: dict[str, Any],
    *,
    rotate_interval: float = PRESENCE_ROTATE_INTERVAL_SECONDS,
) -> bool:
    """Áp dụng trạng thái Discord tức thì (Smart Merge mặc định hoặc bật tác vụ nền luân phiên mỗi 20s)."""
    if bot is None or not callable(getattr(bot, "change_presence", None)):
        return False
    is_ready_fn = getattr(bot, "is_ready", None)
    if callable(is_ready_fn) and not is_ready_fn():
        return False

    # Luôn dừng vòng lặp luân phiên cũ trước khi áp dụng cấu hình mới
    stop_presence_rotation(bot)

    discord_status, steps, mode = build_presence_plan(presence_cfg)
    try:
        await bot.change_presence(status=discord_status, activity=steps[0])

        if mode == "rotate" and len(steps) > 1:
            bot._presence_rotate_task = asyncio.create_task(
                _presence_rotation_loop(bot, discord_status, steps, rotate_interval)
            )

        log.info(
            "Đã áp dụng trạng thái Discord (mode=%s): status=%s, custom_status=%r, activity_type=%s, activity_text=%r, auto_rotate=%s",
            mode,
            presence_cfg.get("status"),
            presence_cfg.get("custom_status"),
            presence_cfg.get("activity_type"),
            presence_cfg.get("activity_text"),
            bool(presence_cfg.get("auto_rotate", False)),
        )
        return True
    except Exception as exc:
        log.warning("Không thể cập nhật trạng thái Discord ngay lúc này: %s", exc)
        return False
