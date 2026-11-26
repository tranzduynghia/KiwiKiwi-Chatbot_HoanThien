"""Router render các trang HTML giao diện (/, /login, /dashboard) của Web Dashboard."""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import sanitize_https_url
from app.core.settings import DEFAULT_OCR_INSTRUCTION, DEFAULT_SYSTEM_PROMPT
from app.core.sync import WriteBehindSyncCoordinator
from app.features.auth.dependencies import AuthDependencies
from app.features.auth.oauth import DiscordOAuthService
from app.features.monitoring.stats import UsageStats

# Mã phiên bản tài nguyên tĩnh giúp trình duyệt luôn nạp bản JS/CSS mới nhất sau mỗi lần deploy
ASSET_VERSION = str(int(time.time()))


def _extract_bot_branding(bot: Any) -> tuple[str, str]:
    """Lấy ảnh đại diện (128px) và tên hiển thị thực tế của bot Discord nếu đã đăng nhập."""
    bot_user = getattr(bot, "user", None) if bot is not None else None
    if bot_user is None:
        return "", "KiwiKiwi"
    avatar_url = ""
    avatar_asset = getattr(bot_user, "display_avatar", None)
    if avatar_asset is not None:
        with_size = getattr(avatar_asset, "with_size", None)
        sized = with_size(128) if callable(with_size) else avatar_asset
        avatar_url = sanitize_https_url(str(getattr(sized, "url", "") or getattr(avatar_asset, "url", "") or ""))
    display_name = str(
        getattr(bot_user, "display_name", "")
        or getattr(bot_user, "name", "")
        or "KiwiKiwi"
    )
    return avatar_url, display_name


async def _resolve_bot_branding(
    bot: Any,
    usage_stats: Optional[UsageStats],
) -> tuple[str, str]:
    """Ưu tiên lấy avatar trực tiếp từ Discord client; nếu đang khởi động thì dùng bản đã lưu trong `bot_metrics`."""
    avatar_url, display_name = _extract_bot_branding(bot)
    if avatar_url:
        return avatar_url, display_name
    if usage_stats is not None:
        cached_url, cached_name = await usage_stats.get_cached_bot_branding()
        return sanitize_https_url(cached_url) or avatar_url, cached_name or display_name
    return avatar_url, display_name


def _sanitize_login_error(error: Optional[str]) -> Optional[str]:
    """Chuẩn hóa và giới hạn độ dài thông báo lỗi trên `/login` để tránh lợi dụng chuỗi dài gây hiểu lầm."""
    if not error:
        return None
    cleaned = " ".join(str(error).split()).strip()
    return cleaned[:200] if cleaned else None


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
    get_bot: Optional[Callable[[], Any]] = None,
    usage_stats: Optional[UsageStats] = None,
    tunnel_origin_url: str = "http://127.0.0.1:8080",
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
        bot_avatar_url, bot_display_name = await _resolve_bot_branding(
            get_bot() if callable(get_bot) else None,
            usage_stats,
        )
        return _render_template(
            templates,
            request,
            "login.html",
            {
                "error": _sanitize_login_error(error),
                "discord_oauth_enabled": oauth_service.is_enabled(request),
                "db_connected": sync_coordinator.connected,
                "bot_avatar_url": bot_avatar_url,
                "bot_display_name": bot_display_name,
                "asset_version": ASSET_VERSION,
            },
        )

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        user = await auth_deps.get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)
        bot_avatar_url, bot_display_name = await _resolve_bot_branding(
            get_bot() if callable(get_bot) else None,
            usage_stats,
        )
        safe_user = dict(user)
        safe_user["avatar_url"] = sanitize_https_url(str(user.get("avatar_url") or ""))
        return _render_template(
            templates,
            request,
            "dashboard.html",
            {
                "user": safe_user,
                "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
                "default_ocr_instruction": DEFAULT_OCR_INSTRUCTION,
                "bot_avatar_url": bot_avatar_url,
                "bot_display_name": bot_display_name,
                "tunnel_origin_url": tunnel_origin_url,
                "asset_version": ASSET_VERSION,
            },
        )

    return router
