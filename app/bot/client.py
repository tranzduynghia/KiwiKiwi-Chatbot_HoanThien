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


def _build_discord_proxy_kwargs(proxy_url: Optional[str]) -> dict[str, Any]:
    """Chuyển đổi URL proxy (`socks5h://`, `socks5://`, `http://`) thành tham số phù hợp cho `discord.Client` kèm tắt keep-alive cũ."""
    raw = (proxy_url or "").strip()
    if not raw:
        return {}
    has_running_loop = True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        has_running_loop = False
    lowered = raw.lower()
    if lowered.startswith(("socks5h://", "socks5://", "socks4a://", "socks4://")):
        if not has_running_loop:
            # `AskBot.__init__` có thể được gọi trước `asyncio.run()`; connector sẽ được gắn ngay trong `setup_hook()`.
            return {}
        try:
            from aiohttp_socks import ProxyConnector

            rdns = lowered.startswith(("socks5h://", "socks4a://"))
            normalized = raw
            if lowered.startswith("socks5h://"):
                normalized = "socks5://" + raw[len("socks5h://"):]
            elif lowered.startswith("socks4a://"):
                normalized = "socks4://" + raw[len("socks4a://"):]
            connector = ProxyConnector.from_url(
                normalized,
                rdns=rdns,
                force_close=True,
                enable_cleanup_closed=True,
            )
            return {"connector": connector, "proxy": None}
        except Exception as exc:
            log.warning("Không tạo được SOCKS5h ProxyConnector cho Discord (%s) — tạm dùng Direct.", exc)
            return {}
    result: dict[str, Any] = {"proxy": raw}
    if has_running_loop:
        import aiohttp

        result["connector"] = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
    return result


class DndCommandTree(app_commands.CommandTree):
    """Cây lệnh Discord gắn cổng kiểm soát toàn cục (`interaction_check`) chặn người dùng thường khi bot đang ở chế độ bảo trì DND."""

    def __init__(self, client: discord.Client, *, maintenance_gate: Any = None):
        super().__init__(client)
        self._maintenance_gate = maintenance_gate

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Chặn ngay lập tức mọi slash command từ non-admin khi `bot_status == 'dnd'`, cho phép Admin vượt khóa kiểm thử."""
        if self._maintenance_gate is not None:
            return await self._maintenance_gate.check_interaction(interaction)
        return True


class AskBot(discord.Client):
    """Tiến trình bot Discord kiêm máy chủ Web Dashboard FastAPI chạy chung vòng lặp asyncio."""

    def __init__(self, container: Any):
        warp_proxy = getattr(container, "warp_proxy", None)
        if warp_proxy is not None:
            proxy_url = warp_proxy.effective_proxy_url() or None
        else:
            proxy_url = (getattr(container.settings, "outbound_proxy_url", "") or "").strip() or None
        self._active_outbound_proxy: Optional[str] = proxy_url
        super().__init__(intents=discord.Intents.default(), **_build_discord_proxy_kwargs(proxy_url))
        self.container = container
        self.tree = DndCommandTree(self, maintenance_gate=getattr(container, "maintenance_gate", None))
        self.ready_at: Optional[float] = None
        self._cleaned_guild_cmds = False
        self._web_server: Optional[uvicorn.Server] = None
        self._web_task: Optional[asyncio.Task] = None
        self._memory_watchdog_task: Optional[asyncio.Task] = None
        self._atlas_config_watcher_task: Optional[asyncio.Task] = None
        self.container.memory_watchdog.attach_bot(self)
        self.container.atlas_config_watcher.attach_presence_hook(
            lambda presence_cfg: apply_bot_presence(self, presence_cfg)
        )
        if warp_proxy is not None:
            warp_proxy.register_route_hook(self._apply_discord_proxy)
        self._register_commands()

    async def _apply_discord_proxy(self, new_proxy: Optional[str]) -> None:
        """Cập nhật nóng connector/proxy cho `discord.Client.http` với `force_close=True` chống socket keep-alive chết qua WARP NAT."""
        self._active_outbound_proxy = new_proxy or None
        http_client = getattr(self, "http", None)
        if http_client is None:
            return
        kwargs = _build_discord_proxy_kwargs(new_proxy)
        http_client.proxy = kwargs.get("proxy")
        new_connector = kwargs.get("connector")
        if new_connector is not None:
            http_client.connector = new_connector
        elif not new_proxy:
            import aiohttp

            http_client.connector = aiohttp.TCPConnector(limit=0, force_close=True, enable_cleanup_closed=True)

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
        """Khởi động Web Dashboard sớm, kết nối MongoDB Atlas, áp dụng Cloudflare WARP/Tunnel, tải model và đồng bộ slash commands."""
        settings = self.container.settings
        if self._active_outbound_proxy and getattr(self.http, "connector", None) is None:
            await self._apply_discord_proxy(self._active_outbound_proxy)
        await self.start_web()  # Mở cổng sớm để nền tảng host (Northflank/Render) nhận diện service
        await self.init_database()  # Kết nối MongoDB + nạp cấu hình động trước khi sync & chạy

        tunnel_cfg = self.container.config_repo.tunnel_cfg()
        if getattr(self.container, "warp_proxy", None) is not None:
            warp_enabled = bool(tunnel_cfg.get("warp_outbound_enabled", settings.warp_outbound_enabled))
            if warp_enabled:
                # Khởi động/xác minh WARP nền không chặn quá trình đăng nhập Discord Gateway
                self.loop.create_task(self.container.warp_proxy.start_and_connect())
            else:
                await self.container.warp_proxy.set_enabled(False, force_probe=False)
        if tunnel_cfg.get("cf_tunnel_enabled"):
            await self.container.cloudflare_tunnel.apply_config(tunnel_cfg)

        await self.container.model_registry.refresh()

        self.loop.create_task(self.model_refresher())
        self.loop.create_task(self.session_sweeper())
        self.loop.create_task(self.config_sync_worker())
        self._atlas_config_watcher_task = self.loop.create_task(
            self.container.atlas_config_watcher.run_forever(self.is_closed)
        )
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

        warp_proxy = getattr(self.container, "warp_proxy", None)
        outbound_label = (
            f"Cloudflare WARP ({warp_proxy.safe_proxy_url})"
            if warp_proxy is not None and warp_proxy.is_active
            else ("proxy" if settings.outbound_proxy_url else "direct")
        )
        log.info("Đã đồng bộ slash commands: %s — phạm vi: %s", ", ".join("/" + cmd.name for cmd in synced), scope)
        log.info(
            "Phiên bản %s · google-genai %s (API %s) · discord.py %s · pymongo %s · RAM tiến trình: %s · Outbound: %s",
            BOT_VERSION,
            getattr(genai, "__version__", "?"),
            settings.gemini_api_version or "mặc định SDK",
            discord.__version__,
            pymongo.version,
            format_bytes(psutil.Process().memory_info().rss),
            outbound_label,
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
            proxy_headers=False,
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
        if getattr(self.container, "maintenance_gate", None) is not None:
            self.container.maintenance_gate.sync_from_config()
        await self.container.session_repo.archive_expired()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Chặn các tương tác nút bấm / thành phần (component) từ người dùng thường khi đang bật chế độ bảo trì DND."""
        gate = getattr(self.container, "maintenance_gate", None)
        if gate is None or not gate.is_dnd:
            return
        if getattr(interaction, "type", None) == discord.InteractionType.component:
            await gate.check_interaction(interaction)

    async def on_message(self, message: discord.Message) -> None:
        """Chặn nhắc tên bot, trả lời tin nhắn bot hoặc nhắn tin trực tiếp khi đang bật chế độ bảo trì DND."""
        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return
        gate = getattr(self.container, "maintenance_gate", None)
        if gate is None or not gate.is_dnd:
            return

        is_dm = getattr(message, "guild", None) is None
        bot_user = getattr(self, "user", None)
        bot_id = getattr(bot_user, "id", None)
        mentions = getattr(message, "mentions", None) or []
        is_mentioned = bool(bot_id and any(getattr(m, "id", None) == bot_id for m in mentions))

        is_reply_to_bot = False
        ref = getattr(message, "reference", None)
        resolved = getattr(ref, "resolved", None) if ref is not None else None
        if resolved is not None and bot_id and getattr(getattr(resolved, "author", None), "id", None) == bot_id:
            is_reply_to_bot = True

        if not (is_dm or is_mentioned or is_reply_to_bot):
            return

        await gate.check_message(message)

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

        presence_cfg = self.container.config_repo.presence_cfg()
        if getattr(self.container, "maintenance_gate", None) is not None:
            self.container.maintenance_gate.sync_from_config(presence_cfg)
        await apply_bot_presence(self, presence_cfg)
        log.info("Đăng nhập: %s (%d server)", self.user, len(self.guilds))

    async def close(self) -> None:
        """Dừng Cloudflare Tunnel, AtlasConfigWatcher, MemoryWatchdog, tác vụ luân phiên Presence, Web Dashboard, MongoDB Atlas và đóng client Discord."""
        stop_presence_rotation(self)
        if self._atlas_config_watcher_task is not None and not self._atlas_config_watcher_task.done():
            self._atlas_config_watcher_task.cancel()
        if self._memory_watchdog_task is not None and not self._memory_watchdog_task.done():
            self._memory_watchdog_task.cancel()
        await self.container.cloudflare_tunnel.stop()
        if getattr(self.container, "warp_proxy", None) is not None:
            await self.container.warp_proxy.shutdown()
        if self._web_server is not None:
            self._web_server.should_exit = True
        if self._web_task is not None:
            try:
                await asyncio.wait_for(self._web_task, timeout=5)
            except Exception:
                pass
        await self.container.db_manager.close()
        await super().close()
