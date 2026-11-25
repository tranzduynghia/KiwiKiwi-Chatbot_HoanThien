"""Discord Client chính (`AskBot`) tích hợp Web Dashboard Uvicorn và các tác vụ đồng bộ nền."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import discord
import psutil
import pymongo
import uvicorn
from discord import app_commands
from google import genai

from app.bot.error_handler import register_error_handler
from app.bot.presence import apply_bot_presence, stop_presence_rotation
from app.core.settings import BOT_VERSION
from app.core.utils import format_bytes
from app.features.chat.commands import register_chat_commands
from app.features.monitoring.commands import register_monitoring_commands
from app.features.sessions.commands import register_session_commands
from app.web.app import create_dashboard_app

log = logging.getLogger("gemini-bot")


class AskBot(discord.Client):
    """Tiến trình bot Discord kiêm máy chủ Web Dashboard FastAPI chạy chung vòng lặp asyncio."""

    def __init__(self, container: Any):
        super().__init__(intents=discord.Intents.default())
        self.container = container
        self.tree = app_commands.CommandTree(self)
        self.ready_at: Optional[float] = None
        self._cleaned_guild_cmds = False
        self._web_server: Optional[uvicorn.Server] = None
        self._web_task: Optional[asyncio.Task] = None
        self._memory_watchdog_task: Optional[asyncio.Task] = None
        self.container.memory_watchdog.attach_bot(self)
        self._register_commands()

    def _register_commands(self) -> None:
        """Đăng ký toàn bộ slash commands từ các lát cắt tính năng và trình bắt lỗi chung."""
        c = self.container
        register_chat_commands(
            self.tree,
            config_repo=c.config_repo,
            model_registry=c.model_registry,
            model_cooldowns=c.model_cooldowns,
            gemini_service=c.gemini_service,
            session_repo=c.session_repo,
            usage_stats=c.usage_stats,
            user_cooldowns=c.user_cooldowns,
            user_locks=c.user_locks,
            gemini_semaphore=c.gemini_semaphore,
        )
        register_session_commands(
            self.tree,
            session_repo=c.session_repo,
            config_repo=c.config_repo,
            user_locks=c.user_locks,
        )
        register_monitoring_commands(
            self.tree,
            bot=self,
            monitoring_service=c.monitoring_service,
            permission_service=c.permission_service,
            config_repo=c.config_repo,
            model_registry=c.model_registry,
            session_repo=c.session_repo,
            db_manager=c.db_manager,
            usage_stats=c.usage_stats,
        )
        register_error_handler(self.tree)

    async def setup_hook(self) -> None:
        """Khởi động Web Dashboard sớm, kết nối MongoDB Atlas, tải model, bật MemoryWatchdog và đồng bộ slash commands."""
        settings = self.container.settings
        await self.start_web()  # Mở cổng sớm để nền tảng host (Northflank/Render) nhận diện service
        await self.init_database()  # Kết nối MongoDB + nạp cấu hình động trước khi sync & chạy
        await self.container.model_registry.refresh()

        self.loop.create_task(self.model_refresher())
        self.loop.create_task(self.session_sweeper())
        self.loop.create_task(self.config_sync_worker())
        self._memory_watchdog_task = self.loop.create_task(
            self.container.memory_watchdog.run_forever(self.is_closed)
        )

        if settings.guild_id:
            guild = discord.Object(id=int(settings.guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            scope = f"CHỈ server có ID {settings.guild_id} (đang đặt GUILD_ID)"
        else:
            synced = await self.tree.sync()
            scope = "TOÀN CỤC (mọi server, có thể mất vài phút để Discord cập nhật)"

        log.info("Đã đồng bộ slash commands: %s — phạm vi: %s", ", ".join("/" + cmd.name for cmd in synced), scope)
        log.info(
            "Phiên bản %s · google-genai %s (API %s) · discord.py %s · pymongo %s · RAM tiến trình: %s",
            BOT_VERSION,
            getattr(genai, "__version__", "?"),
            settings.gemini_api_version or "mặc định SDK",
            discord.__version__,
            pymongo.version,
            format_bytes(psutil.Process().memory_info().rss),
        )

    async def start_web(self) -> None:
        """Khởi chạy máy chủ Uvicorn nhúng phục vụ Web Dashboard và endpoint `/health`."""
        web_app = create_dashboard_app(self.container, get_bot=lambda: self)
        uv_cfg = uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=self.container.settings.port,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(uv_cfg)
        # Để discord.py quản lý tín hiệu dừng tiến trình (SIGINT/SIGTERM)
        server.install_signal_handlers = lambda: None
        self._web_server = server
        self._web_task = self.loop.create_task(server.serve())
        log.info("Web Dashboard + /health chạy tại cổng %d", self.container.settings.port)

    async def init_database(self) -> None:
        """Kết nối MongoDB Atlas, gắn database vào toàn bộ kho dữ liệu và quét phiên hết hạn."""
        db = await self.container.db_manager.connect()
        if db is None:
            return
        self.container.usage_stats.attach_db(db)
        await self.container.session_repo.attach_db(db)
        await self.container.sync_coordinator.attach_db(db)
        await self.container.session_repo.archive_expired()

    async def model_refresher(self) -> None:
        """Tác vụ nền định kỳ tải lại danh sách model từ Gemini API theo `model_refresh_hours`."""
        while not self.is_closed():
            hours = max(0.25, float(self.container.config_repo.models_cfg().get("model_refresh_hours", 6.0)))
            await asyncio.sleep(hours * 3600)
            await self.container.model_registry.refresh()

    async def config_sync_worker(self) -> None:
        """Kiểm tra MongoDB Atlas mỗi 5 giây: tự kết nối lại nếu đứt và xả bộ đệm RAM lên Atlas."""
        while not self.is_closed():
            await asyncio.sleep(5)
            try:
                if self.container.db_manager.enabled and not self.container.sync_coordinator.connected:
                    await self.init_database()
                if self.container.sync_coordinator.connected and self.container.sync_coordinator.pending_count > 0:
                    await self.container.sync_coordinator.flush_pending()
                if self.container.session_repo.ok and self.container.session_repo.pending_turn_count > 0:
                    await self.container.session_repo.flush_pending_bulk()
            except Exception as exc:
                log.warning("Lỗi vòng lặp đồng bộ bộ đệm RAM -> MongoDB Atlas: %s", exc)

    async def session_sweeper(self) -> None:
        """Tác vụ nền định kỳ quét và lưu trữ vĩnh viễn các phiên chat đã quá hạn nghỉ."""
        while not self.is_closed():
            mins = max(1.0, float(self.container.config_repo.session_cfg().get("sweep_minutes", 5.0)))
            await asyncio.sleep(mins * 60)
            await self.container.session_repo.archive_expired()

    async def cleanup_guild_commands(self) -> None:
        """Dọn sạch các slash command cũ gắn riêng từng server khi chuyển sang chế độ đồng bộ toàn cục."""
        if self.container.settings.guild_id:
            return
        for guild in list(self.guilds):
            try:
                if await self.tree.fetch_commands(guild=guild):
                    self.tree.clear_commands(guild=guild)
                    await self.tree.sync(guild=guild)
                    log.info("Đã xoá lệnh riêng còn sót ở server %s (%d)", guild.name, guild.id)
            except discord.HTTPException as exc:
                log.warning("Không dọn được lệnh riêng ở server %s: %s", guild.id, exc)
            await asyncio.sleep(1)

    async def on_ready(self) -> None:
        """Ghi nhận thời điểm sẵn sàng, lưu avatar bot vào `bot_metrics`, khôi phục Presence và dọn lệnh riêng."""
        if not self._cleaned_guild_cmds:
            self._cleaned_guild_cmds = True
            self.loop.create_task(self.cleanup_guild_commands())
        self.ready_at = self.ready_at or time.time()

        # Lưu ảnh đại diện và tên hiển thị thực tế của bot vào MongoDB để trang /login hiển thị ngay cả lúc khởi động
        if self.user is not None:
            avatar_asset = getattr(self.user, "display_avatar", None)
            avatar_url = ""
            if avatar_asset is not None:
                with_size = getattr(avatar_asset, "with_size", None)
                sized = with_size(128) if callable(with_size) else avatar_asset
                avatar_url = str(getattr(sized, "url", "") or getattr(avatar_asset, "url", "") or "")
            bot_name = str(
                getattr(self.user, "display_name", "")
                or getattr(self.user, "name", "")
                or "KiwiKiwi"
            )
            await self.container.usage_stats.save_bot_branding(avatar_url, bot_name)

        await apply_bot_presence(self, self.container.config_repo.presence_cfg())
        log.info("Đăng nhập: %s (%d server)", self.user, len(self.guilds))

    async def close(self) -> None:
        """Dừng MemoryWatchdog, tác vụ luân phiên Presence, Web Dashboard, MongoDB Atlas và đóng client Discord."""
        stop_presence_rotation(self)
        if self._memory_watchdog_task is not None and not self._memory_watchdog_task.done():
            self._memory_watchdog_task.cancel()
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self._web_task is not None:
            try:
                await asyncio.wait_for(self._web_task, timeout=5)
            except Exception:
                pass
        await self.container.db_manager.close()
        await super().close()
