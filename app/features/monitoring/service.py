"""Dịch vụ tổng hợp ảnh chụp trạng thái thời gian thực cho Dashboard và kiểm tra sức khỏe."""
from __future__ import annotations

import asyncio
import platform
import time
from typing import Any, Optional

import discord

from app.core.database import MongoDatabaseManager
from app.core.settings import BOT_VERSION
from app.core.sync import WriteBehindSyncCoordinator
from app.core.utils import format_duration
from app.features.config.repository import ConfigRepository
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.registry import ModelRegistry
from app.features.monitoring.stats import UsageStats
from app.features.monitoring.system_metrics import read_memory_info, read_process_metrics
from app.features.sessions.repository import SessionRepository


class MonitoringService:
    """Tổng hợp trạng thái bot Discord, tài nguyên RAM/CPU, Gemini API, MongoDB và thống kê sử dụng."""

    def __init__(
        self,
        *,
        config_repo: ConfigRepository,
        model_registry: ModelRegistry,
        model_cooldowns: ModelCooldownTracker,
        session_repo: SessionRepository,
        db_manager: MongoDatabaseManager,
        sync_coordinator: WriteBehindSyncCoordinator,
        usage_stats: UsageStats,
        start_time: Optional[float] = None,
    ):
        self._config = config_repo
        self._models = model_registry
        self._model_cooldowns = model_cooldowns
        self._sessions = session_repo
        self._db = db_manager
        self._sync = sync_coordinator
        self._stats = usage_stats
        self.start_time = start_time if start_time is not None else time.time()

    def uptime_seconds(self) -> float:
        """Trả về số giây tiến trình đã hoạt động liên tục."""
        return time.time() - self.start_time

    async def build_status_snapshot(self, bot: Any, *, check_gemini: bool = False) -> dict[str, Any]:
        """Tạo cấu trúc JSON trạng thái toàn hệ thống phục vụ API `/api/status` trên Dashboard."""
        scfg = self._config.session_cfg()
        mem = read_memory_info(scfg.get("ram_limit_mb"))
        proc_metrics = read_process_metrics()
        ram_pct = round(mem["used"] / mem["limit"] * 100, 1) if mem["limit"] else 0.0
        uptime_sec = int(self.uptime_seconds())

        db_ok, db_ms, db_msg = await self._db.ping()
        active = archived = storage = None
        if db_ok:
            active, archived, storage = await asyncio.gather(
                self._sessions.active_count(),
                self._sessions.archive_count(),
                self._db.storage_mb(),
            )
        quota_mb = float(scfg.get("mongodb_quota_mb", 512.0))

        gemini_check = None
        if check_gemini:
            g_ok, g_ms, g_msg = await self._models.check_health()
            gemini_check = {"ok": g_ok, "latency_ms": round(g_ms, 1), "message": g_msg}

        chain_items = [
            {
                "id": mid,
                "display": self._models.display_name_for(mid),
                "cooldown_left": round(self._model_cooldowns.cooldown_left(mid), 1),
            }
            for mid in self._models.auto_chain()
        ]

        bot_ready = bool(bot.is_ready()) if bot is not None else False
        bot_user = getattr(bot, "user", None)
        bot_guilds = getattr(bot, "guilds", []) or []
        bot_ready_at = getattr(bot, "ready_at", None)
        bot_latency = getattr(bot, "latency", 0.0)

        guilds_list = [
            {"id": str(guild.id), "name": guild.name, "member_count": guild.member_count or 0}
            for guild in sorted(bot_guilds, key=lambda item: item.name.lower())
        ]

        return {
            "bot": {
                "online": bot_ready,
                "username": str(bot_user) if bot_user else "Đang khởi động...",
                "bot_id": str(bot_user.id) if bot_user else "",
                "ping_ms": round(bot_latency * 1000, 1) if bot_ready else None,
                "uptime_seconds": uptime_sec,
                "uptime_human": format_duration(uptime_sec),
                "ready_at": int(bot_ready_at) if bot_ready_at else None,
                "guild_count": len(bot_guilds),
                "guilds": guilds_list,
                "version": BOT_VERSION,
                "python_version": platform.python_version(),
                "discord_py_version": discord.__version__,
            },
            "resources": {
                "ram_used_mb": round(mem["used"] / 1024 / 1024, 1),
                "ram_limit_mb": round(mem["limit"] / 1024 / 1024, 1),
                "ram_rss_mb": round(mem["rss"] / 1024 / 1024, 1),
                "ram_percent": ram_pct,
                "cpu_percent": proc_metrics["cpu_percent"],
                "threads": proc_metrics["threads"],
            },
            "gemini": {
                "discovered_count": len(self._models.text),
                "updated_at": int(self._models.updated_at) if self._models.updated_at else None,
                "last_error": self._models.last_error,
                "auto_chain": chain_items,
                "check": gemini_check,
            },
            "mongodb": {
                "enabled": self._db.enabled,
                "ok": db_ok,
                "latency_ms": round(db_ms, 1),
                "message": db_msg,
                "active_sessions": active,
                "archived_sessions": archived,
                "storage_mb": round(storage, 2) if storage is not None else None,
                "quota_mb": quota_mb,
                "last_error": self._sessions.last_error or self._db.last_error,
                "sync": self._sync.sync_status(),
            },
            "stats": self._stats.to_dict(),
        }
