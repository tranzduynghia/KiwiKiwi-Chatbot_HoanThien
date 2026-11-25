"""Router FastAPI cho endpoint `/health` (Northflank/Render/UptimeRobot) và `/api/status`."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.features.monitoring.service import MonitoringService


def create_monitoring_router(
    monitoring_service: MonitoringService,
    get_bot: Callable[[], Any],
    auth_deps: Any,
) -> APIRouter:
    """Tạo router phục vụ kiểm tra sức khỏe tiến trình và giám sát trạng thái thời gian thực."""
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

    return router
