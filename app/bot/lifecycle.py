"""Bộ điều khiển vòng đời Bot: Hot-Reload module lệnh an toàn (kèm dọn RAM) và Soft-Restart Container mượt mà."""
from __future__ import annotations

import asyncio
import gc
import importlib
import logging
import os
import sys
import time
import weakref
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import discord
import psutil

from app.features.audit.service import AuditService

log = logging.getLogger("gemini-bot.lifecycle")

COMMAND_MODULE_NAMES: tuple[str, ...] = (
    "app.features.chat.commands",
    "app.features.monitoring.commands",
    "app.features.sessions.commands",
    "app.features.config.commands",
)

RELOAD_COOLDOWN_SECONDS = 15.0
RESTART_COOLDOWN_SECONDS = 30.0
DEFAULT_RESTART_DELAY_SECONDS = 1.0


class LifecycleCooldownError(RuntimeError):
    """Ngoại lệ phát sinh khi thao tác Hot-Reload hoặc Soft-Restart đang trong thời gian chờ hoặc đã được kích hoạt."""

    def __init__(self, message: str, *, retry_after: float = 0.0, status_code: int = 429) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))
        self.status_code = int(status_code)


def read_process_rss_mb() -> float:
    """Đọc dung lượng RAM tiến trình (RSS) theo MB qua `psutil` (dự phòng `/proc/self/statm`)."""
    try:
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        try:
            with open("/proc/self/statm", "r", encoding="utf-8") as fp:
                parts = fp.read().split()
            if len(parts) >= 2:
                rss_pages = int(parts[1])
                page_size = os.sysconf("SC_PAGE_SIZE")
                return round((rss_pages * page_size) / (1024 * 1024), 2)
        except Exception:
            pass
    return 0.0


class LifecycleController:
    """Điều phối Hot-Reload module lệnh (giữ Gateway/WARP/Tunnel/MongoDB) và Soft-Restart sạch RAM cho container 512MB."""

    def __init__(
        self,
        *,
        audit_service: AuditService,
        sync_coordinator: Any = None,
        session_repo: Any = None,
        reload_cooldown_seconds: float = RELOAD_COOLDOWN_SECONDS,
        restart_cooldown_seconds: float = RESTART_COOLDOWN_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], Any] = os._exit,
        rss_reader: Callable[[], float] = read_process_rss_mb,
    ) -> None:
        self._audit = audit_service
        self._sync = sync_coordinator
        self._sessions = session_repo
        self._reload_cooldown = max(0.0, float(reload_cooldown_seconds))
        self._restart_cooldown = max(0.0, float(restart_cooldown_seconds))
        self._monotonic = monotonic_fn
        self._exit_fn = exit_fn
        self._rss_reader = rss_reader

        self._reload_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._last_reload_at: Optional[float] = None
        self._last_restart_at: Optional[float] = None
        self._restart_in_progress: bool = False
        self._restart_task: Optional[asyncio.Task] = None
        self._tracked_views: weakref.WeakSet[Any] = weakref.WeakSet()

    def track_view(self, view: Any) -> Any:
        """Đăng ký `discord.ui.View` đang mở để tự động dọn sạch khi Hot-Reload tránh rò rỉ RAM."""
        if view is not None:
            try:
                self._tracked_views.add(view)
            except TypeError:
                pass
        return view

    def stop_tracked_views(self) -> int:
        """Dừng toàn bộ `discord.ui.View` còn sót trước khi nạp lại module lệnh."""
        stopped = 0
        for view in list(self._tracked_views):
            try:
                stop_fn = getattr(view, "stop", None)
                if callable(stop_fn):
                    stop_fn()
                    stopped += 1
            except Exception:
                pass
        self._tracked_views.clear()
        return stopped

    async def reload_command_modules(
        self,
        bot: Any,
        *,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        source: str = "web_dashboard",
    ) -> dict[str, Any]:
        """Nạp lại an toàn 4 module lệnh Discord mà không ngắt Gateway, WARP, Tunnel hay MongoDB."""
        if self._reload_lock.locked():
            raise LifecycleCooldownError("Đang có tiến trình Reload Module Lệnh chạy. Vui lòng đợi hoàn tất.", retry_after=2.0)

        async with self._reload_lock:
            now = self._monotonic()
            if self._last_reload_at is not None:
                elapsed = now - self._last_reload_at
                if elapsed < self._reload_cooldown:
                    remaining = round(self._reload_cooldown - elapsed, 1)
                    raise LifecycleCooldownError(
                        f"Vui lòng đợi {remaining}s trước khi Reload Module Lệnh lần tiếp theo.",
                        retry_after=remaining,
                    )

            t0 = time.perf_counter()
            rss_before_mb = float(self._rss_reader())
            stopped_views = self.stop_tracked_views()

            # 1) Dọn sạch lệnh đã đăng ký trên CommandTree trước khi nạp lại để tránh lỗi trùng lệnh
            tree = getattr(bot, "tree", None) if bot is not None else None
            guild_id = ""
            if bot is not None and getattr(bot, "container", None) is not None:
                guild_id = str(getattr(bot.container.settings, "guild_id", "") or "").strip()

            if tree is not None:
                tree.clear_commands(guild=None)
                if guild_id.isdigit():
                    tree.clear_commands(guild=discord.Object(id=int(guild_id)))

            # 2) Chỉ importlib.reload các module lệnh tương tác (tuyệt đối không động tới Gateway/WARP/Tunnel/DB)
            reloaded_modules: list[str] = []
            for mod_name in COMMAND_MODULE_NAMES:
                mod = sys.modules.get(mod_name)
                if mod is None:
                    mod = importlib.import_module(mod_name)
                else:
                    mod = importlib.reload(mod)
                reloaded_modules.append(mod_name)

            # 3) Đăng ký lại toàn bộ slash commands và đồng bộ lại cây lệnh nếu bot đã sẵn sàng
            synced_count = 0
            if bot is not None:
                register_fn = getattr(bot, "register_all_commands", None) or getattr(bot, "_register_commands", None)
                if callable(register_fn):
                    register_fn()

                if tree is not None and bool(getattr(bot, "is_ready", lambda: False)()):
                    try:
                        if guild_id.isdigit():
                            guild_obj = discord.Object(id=int(guild_id))
                            tree.copy_global_to(guild=guild_obj)
                            synced = await tree.sync(guild=guild_obj)
                        else:
                            synced = await tree.sync()
                        synced_count = len(synced)
                    except Exception as exc:
                        log.warning("Không đồng bộ được cây lệnh Discord sau Hot-Reload: %s", type(exc).__name__)

            # 4) Thu gom rác Python và đo RSS sau khi dọn RAM
            gc_collected = gc.collect()
            rss_after_mb = float(self._rss_reader())
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            rss_delta_mb = round(rss_after_mb - rss_before_mb, 2)
            self._last_reload_at = self._monotonic()

            now_iso = datetime.now(timezone.utc).isoformat()
            log.info(
                "[COMMAND_MODULES_RELOADED] count=%d duration_ms=%.1f rss_before=%.2fMB rss_after=%.2fMB actor=%s source=%s",
                len(reloaded_modules),
                duration_ms,
                rss_before_mb,
                rss_after_mb,
                actor,
                source,
            )

            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="COMMAND_MODULES_RELOADED",
                section="system",
                summary=(
                    f"Hot-Reload {len(reloaded_modules)} module lệnh trong {duration_ms:.0f}ms "
                    f"(RSS: {rss_before_mb:.1f} MB → {rss_after_mb:.1f} MB, GC: {gc_collected})"
                ),
                changes=[
                    {"field": "lifecycle.reloaded_modules", "before": "active", "after": ", ".join(reloaded_modules)},
                    {"field": "lifecycle.rss_mb", "before": f"{rss_before_mb:.2f} MB", "after": f"{rss_after_mb:.2f} MB"},
                    {"field": "lifecycle.duration_ms", "before": None, "after": duration_ms},
                ],
                timestamp=now_iso,
                source=source,
                extra_fields={
                    "reloaded_modules": reloaded_modules,
                    "rss_before_mb": rss_before_mb,
                    "rss_after_mb": rss_after_mb,
                    "duration_ms": duration_ms,
                },
                flush=True,
            )

            return {
                "ok": True,
                "reloaded_modules": reloaded_modules,
                "reloaded_count": len(reloaded_modules),
                "stopped_views": stopped_views,
                "synced_commands": synced_count,
                "gc_collected": gc_collected,
                "rss_before_mb": rss_before_mb,
                "rss_after_mb": rss_after_mb,
                "rss_delta_mb": rss_delta_mb,
                "duration_ms": duration_ms,
            }

    async def schedule_soft_restart(
        self,
        bot: Any,
        *,
        actor: str,
        actor_type: str = "dashboard",
        actor_meta: Optional[dict[str, Any]] = None,
        source: str = "web_dashboard",
        delay_seconds: float = DEFAULT_RESTART_DELAY_SECONDS,
    ) -> dict[str, Any]:
        """Ghi Audit Log, trả phản hồi xác nhận trước, sau đó đóng sạch Bot/DB và thoát mã 0 để Northflank khởi động lại container sạch RAM."""
        async with self._restart_lock:
            if self._restart_in_progress:
                raise LifecycleCooldownError(
                    "Lệnh Soft Restart container đã được ghi nhận và đang thực thi.",
                    retry_after=self._restart_cooldown,
                    status_code=409,
                )
            now = self._monotonic()
            if self._last_restart_at is not None:
                elapsed = now - self._last_restart_at
                if elapsed < self._restart_cooldown:
                    remaining = round(self._restart_cooldown - elapsed, 1)
                    raise LifecycleCooldownError(
                        f"Vui lòng đợi {remaining}s trước khi kích hoạt Soft Restart tiếp theo.",
                        retry_after=remaining,
                        status_code=429,
                    )

            self._restart_in_progress = True
            self._last_restart_at = now
            safe_delay = max(0.05, min(10.0, float(delay_seconds)))
            now_iso = datetime.now(timezone.utc).isoformat()
            rss_mb = float(self._rss_reader())

            log.warning(
                "[CONTAINER_SOFT_RESTART] actor=%s source=%s delay_seconds=%.2f rss=%.2fMB",
                actor,
                source,
                safe_delay,
                rss_mb,
            )

            # Ghi và xả ngay Audit Log lên MongoDB Atlas TRƯỚC khi tiến hành thoát tiến trình
            await self._audit.record(
                actor=actor,
                actor_type=actor_type,
                actor_meta=actor_meta,
                action="CONTAINER_SOFT_RESTART",
                section="system",
                summary=f"Kích hoạt Soft Restart container làm sạch RAM (RSS hiện tại: {rss_mb:.1f} MB)",
                changes=[
                    {"field": "lifecycle.container_state", "before": f"running ({rss_mb:.1f} MB)", "after": "soft_restarting"},
                    {"field": "lifecycle.delay_seconds", "before": None, "after": safe_delay},
                ],
                timestamp=now_iso,
                source=source,
                extra_fields={
                    "rss_mb": rss_mb,
                    "delay_seconds": safe_delay,
                },
                flush=True,
            )

            async def _perform_graceful_exit() -> None:
                try:
                    await asyncio.sleep(safe_delay)
                    self.stop_tracked_views()
                    if self._sessions is not None and hasattr(self._sessions, "flush_pending_bulk"):
                        try:
                            await asyncio.wait_for(self._sessions.flush_pending_bulk(), timeout=3.0)
                        except Exception:
                            pass
                    if self._sync is not None and hasattr(self._sync, "flush_pending"):
                        try:
                            await asyncio.wait_for(self._sync.flush_pending(), timeout=3.0)
                        except Exception:
                            pass
                    if bot is not None and callable(getattr(bot, "close", None)):
                        try:
                            await asyncio.wait_for(bot.close(), timeout=6.0)
                        except Exception as exc:
                            log.warning("Lỗi nhẹ khi đóng Discord client trước Soft Restart: %s", type(exc).__name__)
                finally:
                    self._exit_fn(0)

            loop = asyncio.get_running_loop()
            self._restart_task = loop.create_task(_perform_graceful_exit())

            return {
                "ok": True,
                "accepted": True,
                "delay_seconds": safe_delay,
                "rss_mb": rss_mb,
                "message": "Đã ghi nhận lệnh Soft Restart. Container sẽ khởi động lại sạch RAM sau 1 giây...",
            }
