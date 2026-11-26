"""Factory khởi tạo ứng dụng FastAPI, gắn tài nguyên tĩnh và lắp ghép router các tính năng."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hmac
import ipaddress
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.bot.presence import apply_bot_presence, stop_presence_rotation
from app.core.security import is_trusted_proxy_ip
from app.features.audit.router import create_audit_router
from app.features.auth.router import create_auth_router
from app.features.config.router import create_config_router
from app.features.models.router import create_models_router
from app.features.monitoring.router import create_monitoring_router
from app.features.permissions.router import create_permissions_router
from app.features.sessions.router import create_sessions_router
from app.web.pages import create_pages_router
from app.web.static_assets import create_sanitized_static_router

log = logging.getLogger("gemini-bot.web")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

MAX_REQUEST_BODY_BYTES = 256 * 1024  # 256 KB chống tấn công tràn RAM trên container 488MB
CF_RAY_RE = re.compile(r"^[A-Za-z0-9._-]{4,128}$")

DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' https://cdn.discordapp.com https://media.discordapp.net data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def _has_valid_cloudflare_headers(request: Request) -> bool:
    """Xác thực header cạnh Cloudflare (Cf-Ray / CF-Connecting-IP) và tùy chọn header bí mật nội bộ."""
    cf_ray = (request.headers.get("cf-ray") or "").strip()
    cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()

    ray_valid = bool(cf_ray and CF_RAY_RE.fullmatch(cf_ray))
    ip_valid = False
    if cf_ip:
        try:
            ipaddress.ip_address(cf_ip)
            ip_valid = True
        except ValueError:
            ip_valid = False

    if not (ray_valid or ip_valid):
        return False

    # Tùy chọn tăng cường: kiểm tra thêm header bí mật do Cloudflare Transform Rule / Origin Rule chèn
    secret_header_name = (os.getenv("CLOUDFLARE_TUNNEL_SECRET_HEADER") or "").strip()
    secret_header_value = (os.getenv("CLOUDFLARE_TUNNEL_SECRET_VALUE") or "").strip()
    if secret_header_name and secret_header_value:
        provided = (request.headers.get(secret_header_name) or "").strip()
        if not provided or not hmac.compare_digest(provided, secret_header_value):
            return False

    return True


def create_dashboard_app(container: Any, get_bot: Callable[[], Any]) -> FastAPI:
    """Lắp ghép FastAPI app từ container phụ thuộc và gắn toàn bộ router của từng tính năng."""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        yield
        stop_presence_rotation(get_bot())
        if getattr(container, "cloudflare_tunnel", None) is not None:
            await container.cloudflare_tunnel.stop()

    app = FastAPI(
        title="KiwiKiwi Bot Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.exception_handler(Exception)
    async def safe_unhandled_exception_handler(request: Request, exc: Exception):
        """Ngăn rò rỉ traceback hoặc thông tin nhạy cảm khi phát sinh lỗi 500 không mong muốn."""
        log.exception("Lỗi nội bộ chưa được bắt trên %s %s", request.method, request.url.path)
        detail = "Đã xảy ra lỗi nội bộ máy chủ."
        if request.url.path.startswith("/api/") or request.url.path.startswith("/auth/"):
            return JSONResponse({"detail": detail}, status_code=500)
        return PlainTextResponse(detail, status_code=500)

    @app.middleware("http")
    async def security_and_tunnel_middleware(request: Request, call_next):
        """Thực thi biên tin cậy Cloudflare Tunnel, tùy chọn proxy_headers động, giới hạn payload và HTTP Security Headers."""
        current_config = container.config_repo.current_config
        tunnel_cfg = current_config.get("tunnel") if isinstance(current_config.get("tunnel"), dict) else {}
        raw_peer_host = request.client.host if request.client else None
        peer_trusted = is_trusted_proxy_ip(raw_peer_host)

        # 1) Kiểm tra biên tin cậy Cloudflare Tunnel (`tunnel_only_inbound`), luôn giữ mở `/health`
        if request.url.path != "/health":
            tunnel_only_inbound = (
                bool(current_config.get("tunnel_only_inbound", False))
                if "tunnel_only_inbound" in current_config
                else bool(tunnel_cfg.get("tunnel_only_inbound", False))
            )
            if tunnel_only_inbound:
                has_valid_cf = _has_valid_cloudflare_headers(request)
                if not (peer_trusted and has_valid_cf):
                    detail = "Truy cập trực tiếp bị chặn: chỉ cho phép truy cập thông qua Cloudflare Tunnel."
                    if request.url.path.startswith("/api/"):
                        return JSONResponse({"detail": detail}, status_code=403)
                    return PlainTextResponse(detail, status_code=403)

        # 1b) Áp dụng động `proxy_headers` (chỉ tin X-Forwarded-Proto / X-Forwarded-For / CF-Connecting-IP từ proxy tin cậy)
        proxy_headers_enabled = (
            bool(current_config.get("proxy_headers", False))
            if "proxy_headers" in current_config
            else bool(tunnel_cfg.get("proxy_headers", False))
        )
        if proxy_headers_enabled and peer_trusted:
            forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if forwarded_proto in ("http", "https"):
                request.scope["scheme"] = forwarded_proto
                if hasattr(request, "_url"):
                    delattr(request, "_url")
            forwarded_ip = (
                (request.headers.get("cf-connecting-ip") or "").strip()
                or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
            )
            if forwarded_ip:
                try:
                    ipaddress.ip_address(forwarded_ip)
                    request.scope["client"] = (forwarded_ip, 0)
                except ValueError:
                    pass

        # 2) Giới hạn kích thước thân request (chống DoS OOM trên container 488MB)
        if request.method in ("POST", "PUT", "PATCH"):
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    if int(content_length) > MAX_REQUEST_BODY_BYTES:
                        return JSONResponse(
                            {"detail": "Kích thước dữ liệu vượt quá giới hạn cho phép (256 KB)."},
                            status_code=413,
                        )
                except ValueError:
                    return JSONResponse({"detail": "Header Content-Length không hợp lệ."}, status_code=400)
            body = await request.body()
            if len(body) > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    {"detail": "Kích thước dữ liệu vượt quá giới hạn cho phép (256 KB)."},
                    status_code=413,
                )

        response = await call_next(request)

        # 3) Gắn các tiêu đề bảo mật HTTP chuẩn
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", DASHBOARD_CSP)
        if container.auth_deps.is_secure_request(request):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        if request.url.path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        elif request.url.path.startswith(("/static/js/", "/static/css/")):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response

    if STATIC_DIR.exists():
        app.include_router(create_sanitized_static_router(STATIC_DIR))

    app.include_router(
        create_pages_router(
            templates=templates,
            auth_deps=container.auth_deps,
            oauth_service=container.oauth_service,
            sync_coordinator=container.sync_coordinator,
            get_bot=get_bot,
            usage_stats=container.usage_stats,
            tunnel_origin_url=container.cloudflare_tunnel.origin_url,
            get_tunnel_running=lambda: bool(container.cloudflare_tunnel.is_running),
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
