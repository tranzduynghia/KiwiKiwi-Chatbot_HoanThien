"""Router FastAPI quản lý API đọc/cập nhật cấu hình /ask, phiên chat, System Instruction, Bot Presence và Cloudflare Tunnel."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException

from app.core.security import sanitize_config_for_response
from app.core.sync import WriteBehindSyncCoordinator
from app.features.config.schemas import (
    AskConfigPayload,
    PresenceConfigPayload,
    PromptConfigPayload,
    TunnelConfigPayload,
)
from app.features.config.service import ConfigService
from app.features.config.validators import (
    sanitize_ask_and_session,
    sanitize_presence_config,
    sanitize_tunnel_config,
)


def _mask_config_secrets(cfg: dict[str, Any]) -> dict[str, Any]:
    """Loại bỏ hoàn toàn `cf_tunnel_token` và mọi trường bí mật/hash trước khi gửi JSON về trình duyệt, chỉ giữ cờ boolean."""
    return sanitize_config_for_response(cfg)


def create_config_router(
    config_service: ConfigService,
    sync_coordinator: WriteBehindSyncCoordinator,
    get_models_snapshot: Callable[[], Awaitable[dict[str, Any]]],
    auth_deps: Any,
    apply_presence_fn: Optional[Callable[[dict[str, Any]], Awaitable[bool]]] = None,
    apply_tunnel_fn: Optional[Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = None,
) -> APIRouter:
    """Tạo router cho `/api/config`, `/api/config/ask`, `/api/config/prompt`, `/api/config/presence` và `/api/config/tunnel`."""
    router = APIRouter(tags=["config"])

    @router.get("/api/config")
    async def api_get_config(_user: dict[str, Any] = Depends(auth_deps.require_user)):
        cfg = await config_service.get_config(force=True)
        models_info = await get_models_snapshot()
        return {"config": _mask_config_secrets(cfg), "models_runtime": models_info}

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
            client_changes=payload.changes,
        )
        return {"ok": True, "config": _mask_config_secrets(updated), "sync": sync_coordinator.sync_status()}

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
            client_changes=payload.changes,
        )
        return {"ok": True, "config": _mask_config_secrets(updated), "sync": sync_coordinator.sync_status()}

    @router.put("/api/config/presence")
    async def api_update_presence_config(
        payload: PresenceConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        try:
            clean_presence = sanitize_presence_config(
                payload.status,
                payload.activity_type,
                payload.activity_text,
                custom_status=payload.custom_status,
                auto_rotate=payload.auto_rotate,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        actor, actor_type = auth_deps.actor_label(user)
        updated = await config_service.update_config(
            {"presence": clean_presence},
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(user),
            section="presence",
            summary="Cập nhật trạng thái hiển thị & hoạt động Bot",
            client_changes=payload.changes,
        )

        applied_live = False
        if apply_presence_fn is not None:
            applied_live = bool(await apply_presence_fn(clean_presence))

        return {
            "ok": True,
            "applied_live": applied_live,
            "config": _mask_config_secrets(updated),
            "sync": sync_coordinator.sync_status(),
        }

    @router.put("/api/config/tunnel")
    async def api_update_tunnel_config(
        payload: TunnelConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Lưu cấu hình Cloudflare Tunnel vào MongoDB, ghi Audit Log và khởi động/tắt nóng tiến trình `cloudflared`."""
        current_cfg = await config_service.get_config(force=True)
        existing_tunnel = current_cfg.get("tunnel") if isinstance(current_cfg.get("tunnel"), dict) else {}
        existing_token = str(existing_tunnel.get("cf_tunnel_token") or "")

        try:
            clean_tunnel = sanitize_tunnel_config(
                cf_tunnel_enabled=payload.cf_tunnel_enabled,
                cf_tunnel_mode=payload.cf_tunnel_mode,
                cf_tunnel_token=payload.cf_tunnel_token,
                tunnel_only_inbound=payload.tunnel_only_inbound,
                existing_token=existing_token,
                clear_token=payload.clear_token,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        actor, actor_type = auth_deps.actor_label(user)
        action_summary = (
            f"Bật/cập nhật Cloudflare Tunnel (chế độ {clean_tunnel['cf_tunnel_mode']})"
            if clean_tunnel["cf_tunnel_enabled"]
            else "Tắt Cloudflare Tunnel"
        )
        updated = await config_service.update_config(
            {"tunnel": clean_tunnel},
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(user),
            section="tunnel",
            summary=action_summary,
            client_changes=payload.changes,
        )

        tunnel_status: Optional[dict[str, Any]] = None
        if apply_tunnel_fn is not None:
            tunnel_status = await apply_tunnel_fn(clean_tunnel)

        return {
            "ok": True,
            "config": _mask_config_secrets(updated),
            "cloudflare_tunnel": tunnel_status,
            "sync": sync_coordinator.sync_status(),
        }

    return router
