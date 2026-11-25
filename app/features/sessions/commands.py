"""Các slash command Discord quản lý phiên chat (`/refresh` và `/session`)."""
from __future__ import annotations

import discord
from discord import app_commands

from app.core.concurrency import UserLockManager
from app.features.config.repository import ConfigRepository
from app.features.sessions.repository import SessionRepository


def register_session_commands(
    tree: app_commands.CommandTree,
    session_repo: SessionRepository,
    config_repo: ConfigRepository,
    user_locks: UserLockManager,
) -> None:
    """Đăng ký các lệnh `/refresh` và `/session` lên cây lệnh Discord."""

    @tree.command(name="refresh", description="Làm mới phiên chat và lưu trữ cuộc trò chuyện hiện tại")
    async def refresh_command(interaction: discord.Interaction):
        if not session_repo.enabled:
            return await interaction.response.send_message("ℹ️ Bot chưa bật bộ nhớ phiên.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = interaction.user.id
        async with user_locks.get(uid):
            info = await session_repo.end_session(uid, "refresh")
        if info:
            msg = (
                f"🔄 Đã làm mới phiên và **lưu trữ** cuộc trò chuyện ({info['turns']} lượt, "
                f"mã `{info['session_id'][:8]}`).\nCâu hỏi tiếp theo sẽ bắt đầu một phiên mới."
            )
        elif not session_repo.ok:
            msg = "⚠️ Không kết nối được MongoDB, chưa làm mới được phiên. Hãy thử lại sau."
        else:
            msg = "ℹ️ Bạn chưa có phiên nào đang hoạt động, câu hỏi tiếp theo sẽ tự tạo phiên mới."
        await interaction.followup.send(msg, ephemeral=True)

    @tree.command(name="session", description="Xem thông tin phiên chat hiện tại của bạn")
    async def session_command(interaction: discord.Interaction):
        if not session_repo.enabled:
            return await interaction.response.send_message("ℹ️ Bot chưa bật bộ nhớ phiên.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await config_repo.get_config()
        max_hist_msgs = int(config_repo.session_cfg().get("max_history_messages", 20))
        async with user_locks.get(interaction.user.id):
            doc = await session_repo.load(interaction.user.id)
        if not session_repo.ok:
            return await interaction.followup.send("⚠️ Không kết nối được MongoDB, thử lại sau.", ephemeral=True)
        if not doc:
            return await interaction.followup.send(
                "ℹ️ Bạn chưa có phiên nào đang hoạt động. Dùng `/ask` để bắt đầu phiên mới.",
                ephemeral=True,
            )

        def _unix_ts(field: str) -> int:
            return int(doc[field].timestamp())

        sid = (doc.get("session_id") or "")[:8] or "—"
        embed = discord.Embed(title="💬 Phiên chat của bạn", color=discord.Color.blurple())
        embed.add_field(name="Mã phiên", value=f"`{sid}`", inline=True)
        embed.add_field(name="Số lượt hỏi đáp", value=f"**{doc.get('turns', 0)}**", inline=True)
        embed.add_field(
            name="Tin nhắn đang nhớ",
            value=f"**{len(doc.get('messages', []))}** / {max_hist_msgs}",
            inline=True,
        )
        embed.add_field(name="Bắt đầu", value=f"<t:{_unix_ts('created_at')}:R>", inline=True)
        embed.add_field(name="Chat gần nhất", value=f"<t:{_unix_ts('last_active')}:R>", inline=True)
        embed.add_field(
            name="Tự lưu trữ + làm mới",
            value=f"<t:{_unix_ts('expire_at')}:R> (nếu không chat thêm)",
            inline=True,
        )
        embed.set_footer(text="Dùng /refresh để lưu trữ và làm mới ngay")
        await interaction.followup.send(embed=embed, ephemeral=True)
