"""Router FastAPI cho endpoint `/health`, `/api/status` và `/api/system/watchdog/test`."""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.core.memory_watchdog import MemoryWatchdog
from app.features.monitoring.service import MonitoringService


def create_monitoring_router(
    monitoring_service: MonitoringService,
    get_bot: Callable[[], Any],
    auth_deps: Any,
    memory_watchdog: Optional[MemoryWatchdog] = None,
) -> APIRouter:
    """Tạo router phục vụ kiểm tra sức khỏe tiến trình, giám sát thời gian thực và mô phỏng MemoryWatchdog."""
    router = APIRouter(tags=["monitoring"])

    @router.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        bot = get_bot()
        return JSONResponse({
            "status": "ok",
            "discord_ready": bool(bot.is_ready()) if bot is not None else False,
            "uptime_seconds": int(monitoring_service.uptime_seconds()),
        })

    @router.get("/api/status")
    async def api_status(
        check_gemini: bool = Query(default=False),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        return await monitoring_service.build_status_snapshot(get_bot(), check_gemini=check_gemini)

    @router.post("/api/system/watchdog/test")
    async def api_test_memory_watchdog(
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Kích hoạt an toàn quy trình 4 bước Sync-Before-Purge của MemoryWatchdog theo yêu cầu Admin."""
        if memory_watchdog is None:
            raise HTTPException(status_code=503, detail="MemoryWatchdog chưa được khởi tạo.")

        actor, actor_type = auth_deps.actor_label(user)
        event = await memory_watchdog.simulate_emergency_cleanup(
            triggered_by={
                "actor": actor,
                "actor_type": actor_type,
                "actor_meta": auth_deps.actor_meta(user),
            }
        )
        return {
            "ok": True,
            "simulated": True,
            "event": event,
            "synced_total": event.get("synced_total", 0),
            "synced_details": event.get("synced_details", {}),
            "purged_total": event.get("purged_total", 0),
            "purged_details": event.get("purged_details", {}),
            "gc_collected": event.get("gc_collected", 0),
            "sync_timed_out": event.get("sync_timed_out", False),
            "memory_watchdog": memory_watchdog.status_snapshot(),
        }

    return router
