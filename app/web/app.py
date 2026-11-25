"""Factory khởi tạo ứng dụng FastAPI, gắn tài nguyên tĩnh và lắp ghép router các tính năng."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.bot.presence import apply_bot_presence, stop_presence_rotation
from app.features.audit.router import create_audit_router
from app.features.auth.router import create_auth_router
from app.features.config.router import create_config_router
from app.features.models.router import create_models_router
from app.features.monitoring.router import create_monitoring_router
from app.features.permissions.router import create_permissions_router
from app.features.sessions.router import create_sessions_router
from app.web.pages import create_pages_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


def create_dashboard_app(container: Any, get_bot: Callable[[], Any]) -> FastAPI:
    """Lắp ghép FastAPI app từ container phụ thuộc và gắn toàn bộ router của từng tính năng."""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        stop_presence_rotation(get_bot())
        if getattr(container, "cloudflare_tunnel", None) is not None:
            await container.cloudflare_tunnel.stop()

    app = FastAPI(title="KiwiKiwi Bot Dashboard", docs_url=None, redoc_url=None, lifespan=_lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.middleware("http")
    async def enforce_cloudflare_tunnel_inbound(request: Request, call_next):
        """Chặn truy cập trực tiếp bỏ qua Cloudflare khi `tunnel_only_inbound` được bật (luôn giữ `/health` cho Northflank)."""
        if request.url.path != "/health":
            tunnel_cfg = container.config_repo.tunnel_cfg()
            if tunnel_cfg.get("tunnel_only_inbound"):
                has_cf_edge_header = bool(
                    request.headers.get("cf-ray") or request.headers.get("cf-connecting-ip")
                )
                if not has_cf_edge_header:
                    detail = "Truy cập trực tiếp bị chặn: chỉ cho phép truy cập thông qua Cloudflare Tunnel."
                    if request.url.path.startswith("/api/"):
                        return JSONResponse({"detail": detail}, status_code=403)
                    return PlainTextResponse(detail, status_code=403)
        return await call_next(request)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(
        create_pages_router(
            templates=templates,
            auth_deps=container.auth_deps,
            oauth_service=container.oauth_service,
            sync_coordinator=container.sync_coordinator,
            get_bot=get_bot,
            usage_stats=container.usage_stats,
        )
    )
    app.include_router(
        create_monitoring_router(
            monitoring_service=container.monitoring_service,
            get_bot=get_bot,
            auth_deps=container.auth_deps,
            memory_watchdog=container.memory_watchdog,
        )
    )
    app.include_router(
        create_auth_router(
            auth_service=container.auth_service,
            oauth_service=container.oauth_service,
            permission_service=container.permission_service,
            sync_coordinator=container.sync_coordinator,
            auth_deps=container.auth_deps,
        )
    )
    app.include_router(
        create_config_router(
            config_service=container.config_service,
            sync_coordinator=container.sync_coordinator,
            get_models_snapshot=container.model_registry.build_snapshot,
            auth_deps=container.auth_deps,
            apply_presence_fn=lambda presence_cfg: apply_bot_presence(get_bot(), presence_cfg),
            apply_tunnel_fn=container.cloudflare_tunnel.apply_config,
        )
    )
    app.include_router(
        create_models_router(
            model_registry=container.model_registry,
            model_cooldowns=container.model_cooldowns,
            config_service=container.config_service,
            sync_coordinator=container.sync_coordinator,
            auth_deps=container.auth_deps,
        )
    )
    app.include_router(
        create_permissions_router(
            permission_service=container.permission_service,
            sync_coordinator=container.sync_coordinator,
            auth_deps=container.auth_deps,
        )
    )
    app.include_router(
        create_sessions_router(
            session_repo=container.session_repo,
            get_bot=get_bot,
            auth_deps=container.auth_deps,
            user_lookup=container.user_lookup,
        )
    )
    app.include_router(
        create_audit_router(
            audit_service=container.audit_service,
            auth_deps=container.auth_deps,
            get_bot=get_bot,
            user_lookup=container.user_lookup,
        )
    )
    return app
