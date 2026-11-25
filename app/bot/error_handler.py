"""Trình xử lý lỗi toàn cục cho toàn bộ slash command trên Discord."""
from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger("gemini-bot.commands")


def register_error_handler(tree: app_commands.CommandTree) -> None:
    """Đăng ký callback bắt lỗi chung cho cây lệnh `app_commands`."""

    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        log.error("Lỗi lệnh: %r", error)
        msg = "⚠️ Có lỗi khi xử lý lệnh, hãy thử lại."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass
