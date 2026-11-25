"""Slash command Discord `/status` hiển thị tình trạng bot, RAM/CPU, Gemini API và MongoDB Atlas."""
from __future__ import annotations

import asyncio
import os
import platform
import time
from typing import Any

import discord
from discord import app_commands

from app.core.database import MongoDatabaseManager
from app.core.settings import BOT_VERSION
from app.core.utils import format_bytes, format_duration, render_progress_bar
from app.features.config.repository import ConfigRepository
from app.features.models.registry import ModelRegistry
from app.features.monitoring.service import MonitoringService
from app.features.monitoring.stats import UsageStats
from app.features.monitoring.system_metrics import read_memory_info, read_process_metrics
from app.features.permissions.service import PermissionService
from app.features.sessions.repository import SessionRepository


def register_monitoring_commands(
    tree: app_commands.CommandTree,
    *,
    bot: Any,
    monitoring_service: MonitoringService,
    permission_service: PermissionService,
    config_repo: ConfigRepository,
    model_registry: ModelRegistry,
    session_repo: SessionRepository,
    db_manager: MongoDatabaseManager,
    usage_stats: UsageStats,
) -> None:
    """Đăng ký lệnh `/status` với kiểm tra phân quyền động từ MongoDB Atlas."""

    @tree.command(name="status", description="Xem tình trạng bot: RAM, uptime, Gemini API, thống kê")
    async def status_command(interaction: discord.Interaction):
        allowed = await permission_service.can_use_status(interaction.user.id)
        if not allowed:
            return await interaction.response.send_message(
                "🔒 Bạn không có quyền dùng lệnh này.",
                ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        await config_repo.get_config()
        scfg = config_repo.session_cfg()
        idle_hours = float(scfg.get("session_idle_hours", 2.0))
        quota_mb = float(scfg.get("mongodb_quota_mb", 512.0))

        (api_ok, api_ms, api_msg), db_info = await asyncio.gather(
            model_registry.check_health(),
            db_manager.ping(),
        )
        mem = read_memory_info(scfg.get("ram_limit_mb"))
        proc_metrics = read_process_metrics()
        pct = mem["used"] / mem["limit"] * 100
        ready_ts = int(bot.ready_at or monitoring_service.start_time)

        color = (
            discord.Color.green()
            if api_ok and pct < 85
            else (discord.Color.orange() if api_ok else discord.Color.red())
        )
        embed = discord.Embed(title="📊 Tình trạng bot", color=color, timestamp=discord.utils.utcnow())

        embed.add_field(
            name="🤖 Bot",
            inline=True,
            value=(
                f"Ping Discord: **{bot.latency * 1000:.0f} ms**\n"
                f"Uptime: **{format_duration(monitoring_service.uptime_seconds())}**\n"
                f"Online từ: <t:{ready_ts}:R>\n"
                f"Server: **{len(bot.guilds)}**"
            ),
        )
        embed.add_field(
            name="💾 RAM",
            inline=True,
            value=(
                f"Container: **{format_bytes(mem['used'])} / {format_bytes(mem['limit'])}**\n"
                f"`{render_progress_bar(pct)}`\n"
                f"Còn dư: **{format_bytes(max(mem['limit'] - mem['used'], 0))}**\n"
                f"Tiến trình bot: {format_bytes(mem['rss'])}"
            ),
        )
        embed.add_field(
            name="⚙️ CPU",
            inline=True,
            value=(
                f"Tiến trình: **{proc_metrics['cpu_percent']:.1f}%**\n"
                f"Luồng: {proc_metrics['threads']}"
            ),
        )

        cache_age = (
            f"{format_duration(time.time() - model_registry.updated_at)} trước"
            if model_registry.updated_at
            else "chưa tải được"
        )
        embed.add_field(
            name="🔌 Gemini API",
            inline=False,
            value=(
                f"Trạng thái: {'🟢 **Hoạt động**' if api_ok else '🔴 **Lỗi**'} ({api_msg}) · {api_ms:.0f} ms\n"
                f"Model tự động: {model_registry.chain_status_text()}\n"
                f"Danh sách model: **{len(model_registry.text)}** model chat (cập nhật {cache_age})"
                + (f"\n⚠️ Lỗi tải model: {model_registry.last_error}" if model_registry.last_error else "")
            ),
        )

        if not db_manager.enabled:
            db_text = "⚪ Chưa bật (thiếu biến `MONGODB_URI`)"
        else:
            db_ok, db_ms, db_msg = db_info
            active = archived = storage = None
            if db_ok:
                active, archived, storage = await asyncio.gather(
                    session_repo.active_count(),
                    session_repo.archive_count(),
                    db_manager.storage_mb(),
                )
            display_or_q = lambda val: "?" if val is None else val  # noqa: E731
            last_db_error = session_repo.last_error or db_manager.last_error
            db_text = (
                f"Trạng thái: {'🟢 **Hoạt động**' if db_ok else '🔴 **Lỗi**'} ({db_msg}) · {db_ms:.0f} ms\n"
                f"Phiên đang hoạt động: **{display_or_q(active)}** · đã lưu trữ: **{display_or_q(archived)}** phiên\n"
                f"Tự lưu trữ + làm mới sau **{idle_hours:g} giờ** không chat"
                + (
                    f"\nDung lượng: **{storage:.1f} / {quota_mb:g} MB** `{render_progress_bar(storage / quota_mb * 100)}`"
                    if storage is not None
                    else ""
                )
                + (f"\n⚠️ Lỗi gần nhất: {last_db_error}" if last_db_error else "")
            )
        embed.add_field(name="🗄️ Phiên chat & lưu trữ (MongoDB Atlas)", inline=False, value=db_text)

        top_models = ", ".join(f"`{mid}` ×{cnt}" for mid, cnt in usage_stats.daily_models.most_common(4)) or "chưa có"
        rate = f"{usage_stats.ok / usage_stats.total * 100:.0f}%" if usage_stats.total else "—"
        feat_summary = " · ".join(f"{name}: {cnt}" for name, cnt in usage_stats.features.most_common()) or "—"
        last_err = (
            f"<t:{int(usage_stats.last_error[0])}:R> — `{usage_stats.last_error[1][:120]}`"
            if usage_stats.last_error
            else "không có"
        )
        embed.add_field(
            name="📈 Sử dụng (từ lúc bot khởi động)",
            inline=False,
            value=(
                f"Tổng request: **{usage_stats.total}** · thành công **{rate}** · lỗi **{usage_stats.errors}**\n"
                f"Hôm nay (giờ Pacific, mốc reset quota): **{usage_stats.today_total()}** — {top_models}\n"
                f"Tính năng: {feat_summary}\n"
                f"Lỗi gần nhất: {last_err}"
            ),
        )

        env_bits = [f"bản {BOT_VERSION}", f"Python {platform.python_version()}", f"discord.py {discord.__version__}"]
        if os.getenv("RENDER_SERVICE_NAME"):
            env_bits.append(f"Render: {os.getenv('RENDER_SERVICE_NAME')}")
        if os.getenv("RENDER_GIT_COMMIT"):
            env_bits.append(f"commit {os.getenv('RENDER_GIT_COMMIT')[:7]}")
        embed.set_footer(text=" · ".join(env_bits) + "  |  Gemini không cung cấp API xem quota còn lại")
        await interaction.followup.send(embed=embed, ephemeral=True)
