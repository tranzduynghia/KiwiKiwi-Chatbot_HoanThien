"""Router FastAPI quản lý API đọc/cập nhật cấu hình /ask, phiên chat và System Instruction."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException

from app.core.sync import WriteBehindSyncCoordinator
from app.features.config.schemas import AskConfigPayload, PromptConfigPayload
from app.features.config.service import ConfigService
from app.features.config.validators import sanitize_ask_and_session


def create_config_router(
    config_service: ConfigService,
    sync_coordinator: WriteBehindSyncCoordinator,
    get_models_snapshot: Callable[[], Awaitable[dict[str, Any]]],
    auth_deps: Any,
) -> APIRouter:
    """Tạo router cho các endpoint `/api/config`, `/api/config/ask`, `/api/config/prompt`."""
    router = APIRouter(tags=["config"])

    @router.get("/api/config")
    async def api_get_config(_user: dict[str, Any] = Depends(auth_deps.require_user)):
        cfg = await config_service.get_config(force=True)
        models_info = await get_models_snapshot()
        return {"config": cfg, "models_runtime": models_info}

    @router.put("/api/config/ask")
    async def api_update_ask_config(
        payload: AskConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        try:
            clean_ask, clean_session = sanitize_ask_and_session(payload.ask or {}, payload.session or {})
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        actor, actor_type = auth_deps.actor_label(user)
        updated = await config_service.update_config(
            {"ask": clean_ask, "session": clean_session},
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(user),
            section="ask",
            summary="Cập nhật cấu hình /ask & phiên chat",
        )
        return {"ok": True, "config": updated, "sync": sync_coordinator.sync_status()}

    @router.put("/api/config/prompt")
    async def api_update_prompt_config(
        payload: PromptConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        actor, actor_type = auth_deps.actor_label(user)
        updated = await config_service.update_config(
            {
                "system_prompt": payload.system_prompt.strip(),
                "ocr_instruction": payload.ocr_instruction.strip(),
                "append_current_date": bool(payload.append_current_date),
            },
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(user),
            section="prompt",
            summary="Cập nhật System Instruction / OCR Instruction",
        )
        return {"ok": True, "config": updated, "sync": sync_coordinator.sync_status()}

    return router
