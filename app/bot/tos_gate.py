"""Cổng xác nhận Điều khoản có phiên bản (Version-Aware ToS Consent Gate) cho Discord Bot."""
from __future__ import annotations

import logging
from typing import Any

import discord

from app.features.legal.defaults import PUBLIC_SITE_ORIGIN
from app.features.legal.service import LegalService

log = logging.getLogger("gemini-bot.tos")

TOS_ACCEPT_BUTTON_CUSTOM_ID = "kiwi_tos_accept_btn"
TOS_CONSENT_VIEW_TIMEOUT_SECONDS = 60.0
TOS_GATED_COMMANDS = frozenset({"ask", "refresh", "session"})


class TosConsentView(discord.ui.View):
    """Giao diện nút tương tác `Tôi đồng ý` (timeout=60s, gọi `self.stop()` để chống rò rỉ RAM trên 488MB)."""

    def __init__(
        self,
        *,
        legal_service: LegalService,
        expected_user_id: int,
        active_version: str,
        timeout: float = TOS_CONSENT_VIEW_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout)
        self._legal_service = legal_service
        self._expected_user_id = int(expected_user_id)
        self._active_version = active_version

    @discord.ui.button(
        label="Tôi đồng ý (Accept Terms)",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=TOS_ACCEPT_BUTTON_CUSTOM_ID,
    )
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Ghi nhận chấp thuận ToS vào MongoDB + RAM cache, cập nhật thông báo và dừng View ngay."""
        user = getattr(interaction, "user", None)
        uid = getattr(user, "id", None)
        if uid is None or int(uid) != self._expected_user_id:
            await interaction.response.send_message(
                "⚠️ Nút xác nhận này dành cho người dùng khác.",
                ephemeral=True,
            )
            return

        display_name = str(
            getattr(user, "display_name", "")
            or getattr(user, "name", "")
            or str(uid)
        )
        await self._legal_service.accept_current_tos(
            user_id=uid,
            username=display_name,
            source="discord",
        )
        button.disabled = True
        try:
            await interaction.response.edit_message(
                content=(
                    f"✅ Cảm ơn bạn đã đồng ý Điều khoản Dịch vụ phiên bản **{self._legal_service.active_tos_version}**!\n"
                    "Bạn có thể sử dụng lệnh `/ask` và trò chuyện với KiwiKiwi ngay bây giờ."
                ),
                view=None,
            )
        except Exception:
            try:
                await interaction.response.send_message(
                    f"✅ Đã xác nhận Điều khoản phiên bản **{self._legal_service.active_tos_version}**. Bạn có thể dùng `/ask` ngay!",
                    ephemeral=True,
                )
            except Exception:
                pass
        self.stop()

    async def on_timeout(self) -> None:
        """Dừng View và giải phóng tham chiếu khi quá 60 giây để tránh rò rỉ bộ nhớ."""
        self.stop()


def build_tos_prompt_message(*, active_version: str, is_update: bool, accepted_version: str | None = None) -> str:
    """Soạn nội dung thông báo yêu cầu chấp thuận Điều khoản (lần đầu hoặc khi nâng phiên bản ToS)."""
    terms_url = f"{PUBLIC_SITE_ORIGIN}/terms"
    policies_url = f"{PUBLIC_SITE_ORIGIN}/policies"
    guidelines_url = f"{PUBLIC_SITE_ORIGIN}/guidelines"

    if is_update:
        header = (
            f"📜 **Điều khoản Dịch vụ đã được cập nhật lên phiên bản `{active_version}`**"
            + (f" *(phiên bản cũ của bạn: `{accepted_version}`)*" if accepted_version else "")
            + "\nVui lòng xem lại và nhấn **Tôi đồng ý** trước khi tiếp tục sử dụng `/ask` hoặc phiên AI:"
        )
    else:
        header = (
            f"📜 **Chào mừng bạn đến với KiwiKiwi Bot!**\n"
            f"Trước khi sử dụng lệnh `/ask` hoặc bắt đầu phiên AI, vui lòng đọc và đồng ý Điều khoản phiên bản `{active_version}`:"
        )

    return (
        f"{header}\n"
        f"• Điều khoản Dịch vụ: <{terms_url}>\n"
        f"• Chính sách Quyền riêng tư: <{policies_url}>\n"
        f"• Nguyên tắc Cộng đồng: <{guidelines_url}>"
    )


class TosConsentGate:
    """Kiểm tra `tos_accepted_version == active_tos_version` trước khi cho phép chạy `/ask` hoặc phiên chat AI."""

    def __init__(self, legal_service: LegalService) -> None:
        self._legal_service = legal_service

    async def check_interaction(self, interaction: discord.Interaction) -> bool:
        """Chặn `/ask` và các lệnh phiên chat nếu người dùng chưa đồng ý `active_tos_version`, gửi prompt `ephemeral=True` kèm nút Tôi đồng ý."""
        cmd = getattr(interaction, "command", None)
        cmd_name = str(getattr(cmd, "name", "") or "").strip().lower()
        if not cmd_name:
            data = getattr(interaction, "data", None)
            if isinstance(data, dict):
                cmd_name = str(data.get("name") or "").strip().lower()

        if cmd_name and cmd_name not in TOS_GATED_COMMANDS:
            return True

        user = getattr(interaction, "user", None)
        user_id = getattr(user, "id", None)
        if user_id is None:
            return True

        # Đường dẫn nhanh O(1) từ bộ đệm RAM (0 độ trễ DB cho người dùng hợp lệ)
        if self._legal_service.has_accepted_current_tos_fast(user_id):
            return True

        status = await self._legal_service.get_user_consent_status(user_id)
        if status["accepted"]:
            return True

        active_ver = status["active_tos_version"]
        prompt_text = build_tos_prompt_message(
            active_version=active_ver,
            is_update=bool(status["is_update"]),
            accepted_version=status.get("accepted_version"),
        )
        view = TosConsentView(
            legal_service=self._legal_service,
            expected_user_id=int(user_id),
            active_version=active_ver,
            timeout=TOS_CONSENT_VIEW_TIMEOUT_SECONDS,
        )
        try:
            response = getattr(interaction, "response", None)
            if response is not None and not response.is_done():
                await response.send_message(prompt_text, view=view, ephemeral=True)
            else:
                followup = getattr(interaction, "followup", None)
                if followup is not None:
                    await followup.send(prompt_text, view=view, ephemeral=True)
        except Exception as exc:
            log.warning("Không gửi được hộp thoại xác nhận ToS cho user_id=%s: %s", user_id, exc)
            view.stop()

        return False

    async def check_message(self, message: discord.Message) -> bool:
        """Chặn DM / nhắc tên / trả lời phiên AI khi người dùng chưa đồng ý `active_tos_version`."""
        author = getattr(message, "author", None)
        user_id = getattr(author, "id", None)
        if user_id is None:
            return True

        if self._legal_service.has_accepted_current_tos_fast(user_id):
            return True

        status = await self._legal_service.get_user_consent_status(user_id)
        if status["accepted"]:
            return True

        active_ver = status["active_tos_version"]
        prompt_text = build_tos_prompt_message(
            active_version=active_ver,
            is_update=bool(status["is_update"]),
            accepted_version=status.get("accepted_version"),
        )
        view = TosConsentView(
            legal_service=self._legal_service,
            expected_user_id=int(user_id),
            active_version=active_ver,
            timeout=TOS_CONSENT_VIEW_TIMEOUT_SECONDS,
        )
        try:
            reply_fn = getattr(message, "reply", None)
            if callable(reply_fn):
                await reply_fn(
                    prompt_text,
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                    delete_after=60,
                )
        except Exception as exc:
            log.warning("Không gửi được thông báo ToS trong on_message cho user_id=%s: %s", user_id, exc)
            view.stop()

        return False
