"""Router render các trang HTML giao diện (/, /login, /dashboard) của Web Dashboard."""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.settings import DEFAULT_OCR_INSTRUCTION, DEFAULT_SYSTEM_PROMPT
from app.core.sync import WriteBehindSyncCoordinator
from app.features.auth.dependencies import AuthDependencies
from app.features.auth.oauth import DiscordOAuthService

# Mã phiên bản tài nguyên tĩnh giúp trình duyệt luôn nạp bản JS/CSS mới nhất sau mỗi lần deploy
ASSET_VERSION = str(int(time.time()))


def _render_template(templates: Jinja2Templates, request: Request, name: str, context: dict[str, Any]):
    """Hỗ trợ tương thích cả phiên bản Starlette/FastAPI cũ và mới khi gọi TemplateResponse."""
    try:
        return templates.TemplateResponse(request=request, name=name, context=context)
    except TypeError:
        return templates.TemplateResponse(name, {"request": request, **context})


def create_pages_router(
    *,
    templates: Jinja2Templates,
    auth_deps: AuthDependencies,
    oauth_service: DiscordOAuthService,
    sync_coordinator: WriteBehindSyncCoordinator,
) -> APIRouter:
    """Tạo router giao diện HTML cho trang gốc, trang đăng nhập và trang quản trị Dashboard."""
    router = APIRouter(tags=["pages"])

    @router.api_route("/", methods=["GET", "HEAD"])
    async def root_page(request: Request):
        if request.method == "HEAD":
            return JSONResponse({"status": "ok"})
        user = await auth_deps.get_current_user(request)
        return RedirectResponse(url="/dashboard" if user else "/login", status_code=302)

    @router.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: Optional[str] = None):
        user = await auth_deps.get_current_user(request)
        if user:
            return RedirectResponse(url="/dashboard", status_code=302)
        return _render_template(
            templates,
            request,
            "login.html",
            {
                "error": error,
                "discord_oauth_enabled": oauth_service.is_enabled(request),
                "db_connected": sync_coordinator.connected,
                "asset_version": ASSET_VERSION,
            },
        )

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        user = await auth_deps.get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)
        return _render_template(
            templates,
            request,
            "dashboard.html",
            {
                "user": user,
                "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
                "default_ocr_instruction": DEFAULT_OCR_INSTRUCTION,
                "asset_version": ASSET_VERSION,
            },
        )

    return router
