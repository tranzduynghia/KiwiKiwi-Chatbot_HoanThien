"""Slash command `/config` dành riêng cho Admin: bảng điều khiển ephemeral hỗ trợ Hot-Reload module lệnh và Soft-Restart Container."""
from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from app.bot.lifecycle import LifecycleController, LifecycleCooldownError

log = logging.getLogger("gemini-bot.config.commands")

CONFIG_VIEW_TIMEOUT_SECONDS = 60.0


async def _is_discord_admin(user_id: int | str, permission_service: Any, maintenance_gate: Any = None) -> bool:
    """Xác thực quyền Admin Discord (ưu tiên `maintenance_gate.is_admin_user` hoặc `permission_service`)."""
    if maintenance_gate is not None:
        try:
            if await maintenance_gate.is_admin_user(user_id):
                return True
        except Exception:
            pass
    if permission_service is not None:
        try:
            perm = await permission_service.get_permission(str(user_id), force=False)
            if isinstance(perm, dict) and bool(perm.get("is_admin")):
                return True
        except Exception:
            pass
    return False


def _actor_info_from_interaction(interaction: discord.Interaction) -> tuple[str, dict[str, Any]]:
    """Tạo chuỗi actor và metadata chuẩn Audit Log từ `discord.Interaction`."""
    user = getattr(interaction, "user", None)
    uid = str(getattr(user, "id", "") or "unknown")
    display = str(getattr(user, "display_name", "") or getattr(user, "name", "") or f"discord:{uid}")
    avatar_asset = getattr(user, "display_avatar", None)
    avatar_url = str(getattr(avatar_asset, "url", "") or "") if avatar_asset is not None else ""
    return (
        f"{display} (discord:{uid})",
        {
            "actor_display_name": display,
            "actor_discord_id": uid,
            "actor_avatar_url": avatar_url,
        },
    )


class LifecycleConfirmView(discord.ui.View):
    """Hộp thoại xác nhận 2 bước (ephemeral, timeout=60s, dừng View ngay sau khi hoàn tất/hết hạn chống rò RAM)."""

    def __init__(
        self,
        *,
        action: str,
        expected_user_id: int,
        bot: Any,
        lifecycle_controller: LifecycleController,
        timeout: float = CONFIG_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._action = action
        self._expected_user_id = int(expected_user_id)
        self._bot = bot
        self._lifecycle = lifecycle_controller
        self._lifecycle.track_view(self)

        if action == "soft_restart":
            self.confirm_button.label = "⚠️ Xác nhận Soft Restart"
            self.confirm_button.style = discord.ButtonStyle.danger
        else:
            self.confirm_button.label = "✅ Xác nhận Reload Lệnh"
            self.confirm_button.style = discord.ButtonStyle.success

    @discord.ui.button(label="Xác nhận", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(getattr(interaction.user, "id", 0)) != self._expected_user_id:
            await interaction.response.send_message("⚠️ Bạn không phải người mở bảng xác nhận này.", ephemeral=True)
            return

        actor, actor_meta = _actor_info_from_interaction(interaction)
        self.stop()

        if self._action == "reload_commands":
            try:
                result = await self._lifecycle.reload_command_modules(
                    self._bot,
                    actor=actor,
                    actor_type="discord",
                    actor_meta=actor_meta,
                    source="discord_slash",
                )
                await interaction.response.edit_message(
                    content=(
                        f"✅ **Đã Hot-Reload {result['reloaded_count']} module lệnh thành công!**\n"
                        f"• Thời gian xử lý: `{result['duration_ms']} ms`\n"
                        f"• RAM tiến trình (RSS): `{result['rss_before_mb']} MB` → `{result['rss_after_mb']} MB` "
                        f"(GC: `{result['gc_collected']}` đối tượng)\n"
                        f"• Kết nối Discord Gateway, WARP, Tunnel & MongoDB vẫn giữ nguyên."
                    ),
                    view=None,
                )
            except LifecycleCooldownError as exc:
                await interaction.response.edit_message(content=f"⏳ {exc}", view=None)
            except Exception as exc:
                log.warning("Lỗi khi Hot-Reload module lệnh từ /config: %s", type(exc).__name__)
                await interaction.response.edit_message(
                    content="❌ Không thể Reload Module Lệnh lúc này. Vui lòng kiểm tra nhật ký hệ thống.",
                    view=None,
                )
            return

        # Soft Restart Container: trả xác nhận ephemeral TRƯỚC khi thực thi thoát tiến trình sau 1 giây
        try:
            await interaction.response.edit_message(
                content="✅ Đã ghi nhận lệnh Soft Restart. Container sẽ khởi động lại sạch RAM sau 1 giây...",
                view=None,
            )
            await self._lifecycle.schedule_soft_restart(
                self._bot,
                actor=actor,
                actor_type="discord",
                actor_meta=actor_meta,
                source="discord_slash",
                delay_seconds=1.0,
            )
        except LifecycleCooldownError as exc:
            await interaction.followup.send(f"⏳ {exc}", ephemeral=True)
        except Exception as exc:
            log.warning("Lỗi khi kích hoạt Soft Restart từ /config: %s", type(exc).__name__)
            await interaction.followup.send("❌ Không thể kích hoạt Soft Restart lúc này.", ephemeral=True)

    @discord.ui.button(label="Hủy bỏ", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(getattr(interaction.user, "id", 0)) != self._expected_user_id:
            await interaction.response.send_message("⚠️ Bạn không phải người mở bảng xác nhận này.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="❎ Đã hủy thao tác quản trị vòng đời Bot.", view=None)

    async def on_timeout(self) -> None:
        self.stop()


class AdminConfigConsoleView(discord.ui.View):
    """Bảng điều khiển `/config` ephemeral dành riêng cho Admin (Reload Module Lệnh & Soft Restart Container)."""

    def __init__(
        self,
        *,
        expected_user_id: int,
        bot: Any,
        lifecycle_controller: LifecycleController,
        timeout: float = CONFIG_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._expected_user_id = int(expected_user_id)
        self._bot = bot
        self._lifecycle = lifecycle_controller
        self._lifecycle.track_view(self)

    @discord.ui.button(label="Reload Module Lệnh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def reload_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(getattr(interaction.user, "id", 0)) != self._expected_user_id:
            await interaction.response.send_message("⚠️ Bảng điều khiển này thuộc về quản trị viên khác.", ephemeral=True)
            return
        self.stop()
        confirm_view = LifecycleConfirmView(
            action="reload_commands",
            expected_user_id=self._expected_user_id,
            bot=self._bot,
            lifecycle_controller=self._lifecycle,
        )
        await interaction.response.edit_message(
            content=(
                "🔄 **Xác nhận Reload Module Lệnh (Giữ kết nối)**\n"
                "Hệ thống sẽ dọn View tồn đọng, nạp lại 4 module lệnh, chạy `gc.collect()` và giữ nguyên kết nối Discord/MongoDB/WARP/Tunnel.\n"
                "Nhấn nút bên dưới trong vòng 60 giây để xác nhận:"
            ),
            view=confirm_view,
        )

    @discord.ui.button(label="Soft Restart Container", style=discord.ButtonStyle.danger, emoji="♻️")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if int(getattr(interaction.user, "id", 0)) != self._expected_user_id:
            await interaction.response.send_message("⚠️ Bảng điều khiển này thuộc về quản trị viên khác.", ephemeral=True)
            return
        self.stop()
        confirm_view = LifecycleConfirmView(
            action="soft_restart",
            expected_user_id=self._expected_user_id,
            bot=self._bot,
            lifecycle_controller=self._lifecycle,
        )
        await interaction.response.edit_message(
            content=(
                "⚠️ **Cảnh báo: Xác nhận Soft Restart Container (Làm sạch RAM)**\n"
                "Bot và Web Dashboard sẽ ngắt kết nối khoảng 3–5 giây để Northflank khởi động lại container sạch RAM.\n"
                "Nhấn nút bên dưới trong vòng 60 giây để xác nhận:"
            ),
            view=confirm_view,
        )

    async def on_timeout(self) -> None:
        self.stop()


def register_config_commands(
    tree: app_commands.CommandTree,
    *,
    bot: Any,
    permission_service: Any,
    lifecycle_controller: LifecycleController,
    maintenance_gate: Any = None,
) -> None:
    """Đăng ký slash command `/config` dành riêng cho Quản trị viên (Admin)."""

    @tree.command(name="config", description="[Admin] Bảng điều khiển vòng đời Bot (Reload Lệnh & Soft Restart Container)")
    async def config_command(interaction: discord.Interaction) -> None:
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if user_id is None or not await _is_discord_admin(user_id, permission_service, maintenance_gate):
            await interaction.response.send_message(
                "⛔ Chỉ Quản trị viên (Admin) mới được phép sử dụng lệnh `/config`.",
                ephemeral=True,
            )
            return

        view = AdminConfigConsoleView(
            expected_user_id=int(user_id),
            bot=bot,
            lifecycle_controller=lifecycle_controller,
        )
        await interaction.response.send_message(
            "⚙️ **Bảng điều khiển Quản trị Vòng đời Bot (`/config`)**\n"
            "• **Reload Module Lệnh**: Nạp nóng các module lệnh + `gc.collect()`, giữ nguyên kết nối.\n"
            "• **Soft Restart Container**: Đóng sạch bot & thoát mã `0` để Northflank tạo lại container sạch RAM.",
            view=view,
            ephemeral=True,
        )
