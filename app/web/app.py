"""Factory khởi tạo ứng dụng FastAPI, gắn tài nguyên tĩnh và lắp ghép router các tính năng."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.features.audit.router import create_audit_router
from app.features.auth.router import create_auth_router
from app.features.config.router import create_config_router
from app.features.models.router import create_models_router
from app.features.monitoring.router import create_monitoring_router
from app.features.permissions.router import create_permissions_router
from app.web.pages import create_pages_router

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


def create_dashboard_app(container: Any, get_bot: Callable[[], Any]) -> FastAPI:
    """Lắp ghép FastAPI app từ container phụ thuộc và gắn toàn bộ router của từng tính năng."""
    app = FastAPI(title="KiwiKiwi Bot Dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(
        create_pages_router(
            templates=templates,
            auth_deps=container.auth_deps,
            oauth_service=container.oauth_service,
            sync_coordinator=container.sync_coordinator,
        )
    )
    app.include_router(
        create_monitoring_router(
            monitoring_service=container.monitoring_service,
            get_bot=get_bot,
            auth_deps=container.auth_deps,
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
        create_audit_router(
            audit_service=container.audit_service,
            auth_deps=container.auth_deps,
        )
    )
    return app
