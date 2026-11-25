"""Router FastAPI cho quản lý model Gemini (tải lại API, xóa cooldown, cập nhật cấu hình model)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.core.sync import WriteBehindSyncCoordinator
from app.features.config.service import ConfigService
from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.registry import ModelRegistry
from app.features.models.schemas import ModelsConfigPayload
from app.features.models.validators import sanitize_models_config


def create_models_router(
    model_registry: ModelRegistry,
    model_cooldowns: ModelCooldownTracker,
    config_service: ConfigService,
    sync_coordinator: WriteBehindSyncCoordinator,
    auth_deps: Any,
) -> APIRouter:
    """Tạo router cho `/api/models/refresh`, `/api/models/clear-cooldowns` và `/api/config/models`."""
    router = APIRouter(tags=["models"])

    @router.post("/api/models/refresh")
    async def api_refresh_models(_user: dict[str, Any] = Depends(auth_deps.require_user)):
        await model_registry.refresh()
        return {"ok": True, "models": await model_registry.build_snapshot()}

    @router.post("/api/models/clear-cooldowns")
    async def api_clear_cooldowns(_user: dict[str, Any] = Depends(auth_deps.require_user)):
        cleared = model_cooldowns.clear_all()
        return {"ok": True, "cleared": cleared}

    @router.put("/api/config/models")
    async def api_update_models_config(
        payload: ModelsConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        clean_models = sanitize_models_config(payload)
        actor, actor_type = auth_deps.actor_label(user)
        updated = await config_service.update_config(
            {"models": clean_models},
            updated_by=actor,
            actor_type=actor_type,
            section="models",
            summary="Cập nhật cấu hình Model Gemini",
        )
        return {
            "ok": True,
            "config": updated,
            "models_runtime": await model_registry.build_snapshot(),
            "sync": sync_coordinator.sync_status(),
        }

    return router
