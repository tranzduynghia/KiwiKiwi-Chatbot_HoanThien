"""Router FastAPI quản lý API đọc/cập nhật cấu hình /ask, phiên chat, System Instruction, Bot Presence và Cloudflare Tunnel."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.core.security import sanitize_config_for_response
from app.core.sync import WriteBehindSyncCoordinator
from app.features.config.schemas import (
    AskConfigPayload,
    DashboardSettingsPayload,
    PresenceConfigPayload,
    PromptConfigPayload,
    TunnelConfigPayload,
    WarpActionPayload,
    WarpConfigPayload,
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
    schedule_tunnel_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    apply_warp_fn: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    start_warp_fn: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    update_warp_fn: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None,
    stop_warp_fn: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None,
    trace_warp_fn: Optional[Callable[[], Awaitable[dict[str, Any]]]] = None,
    get_tunnel_status_fn: Optional[Callable[[], dict[str, Any]]] = None,
    maintenance_gate: Any = None,
) -> APIRouter:
    """Tạo router cho `/api/config*`, `/api/tunnel/update`, `/api/tunnel/status` và `/api/warp/*`."""
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

        before_cfg = await config_service.get_config(force=False)
        before_presence = before_cfg.get("presence") if isinstance(before_cfg.get("presence"), dict) else {}
        previous_status = str(before_presence.get("status") or "online").strip().lower()

        actor, actor_type = auth_deps.actor_label(user)
        actor_meta = auth_deps.actor_meta(user)
        updated = await config_service.update_config(
            {"presence": clean_presence},
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=actor_meta,
            section="presence",
            summary="Cập nhật trạng thái hiển thị & hoạt động Bot",
            client_changes=payload.changes,
        )

        if maintenance_gate is not None:
            maintenance_gate.sync_from_config(clean_presence)
            if previous_status != clean_presence["status"]:
                await maintenance_gate.record_status_transition(
                    from_status=previous_status,
                    to_status=clean_presence["status"],
                    actor=actor,
                    actor_type=actor_type,
                    actor_meta=actor_meta,
                    source="Dashboard",
                    flush=True,
                )

        applied_live = False
        if apply_presence_fn is not None:
            applied_live = bool(await apply_presence_fn(clean_presence))

        return {
            "ok": True,
            "applied_live": applied_live,
            "bot_status": clean_presence["status"],
            "maintenance_mode": clean_presence["status"] == "dnd",
            "config": _mask_config_secrets(updated),
            "sync": sync_coordinator.sync_status(),
        }

    ALLOWED_LOCKABLE_SECTIONS = ("perms", "sessions", "temp", "legal", "audit", "ask", "prompt", "models", "status")

    @router.get("/api/dashboard-settings")
    async def api_get_dashboard_settings(
        _user: dict[str, Any] = Depends(auth_deps.require_step_up_auth),
    ):
        """Đọc cấu hình bảo mật Step-up Auth & chu kỳ Presence (bắt buộc Step-up Auth cứng)."""
        cfg = await config_service.get_config(force=False)
        sec = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
        pres = cfg.get("presence") if isinstance(cfg.get("presence"), dict) else {}
        raw_locked = sec.get("locked_sections") if isinstance(sec.get("locked_sections"), list) else ["perms", "sessions", "temp"]
        locked_sections = [s for s in raw_locked if s in ALLOWED_LOCKABLE_SECTIONS]
        return {
            "ok": True,
            "locked_sections": locked_sections,
            "step_up_cooldown_sec": max(15, min(300, int(sec.get("step_up_cooldown_sec", 60)))),
            "presence_interval_sec": max(15, min(3600, int(sec.get("presence_interval_sec", 20)))),
            "presence_auto_rotate": bool(pres.get("auto_rotate", False)),
            "can_manage": bool(_user.get("is_admin")),
        }

    @router.put("/api/dashboard-settings")
    async def api_update_dashboard_settings(
        payload: DashboardSettingsPayload,
        admin: dict[str, Any] = Depends(auth_deps.require_admin_step_up),
    ):
        """Cập nhật & áp dụng tức thì (live) danh sách mục khóa, cooldown Step-up và chu kỳ Presence."""
        cleaned_sections = []
        seen = set()
        for item in payload.locked_sections:
            key = str(item or "").strip().lower()
            if key in ALLOWED_LOCKABLE_SECTIONS and key not in seen:
                seen.add(key)
                cleaned_sections.append(key)

        cooldown_sec = max(15, min(300, int(payload.step_up_cooldown_sec)))
        interval_sec = max(15, min(3600, int(payload.presence_interval_sec)))

        patch: dict[str, Any] = {
            "security": {
                "locked_sections": cleaned_sections,
                "step_up_cooldown_sec": cooldown_sec,
                "presence_interval_sec": interval_sec,
            }
        }
        if payload.presence_auto_rotate is not None:
            patch["presence"] = {"auto_rotate": bool(payload.presence_auto_rotate)}

        actor, actor_type = auth_deps.actor_label(admin)
        updated = await config_service.update_config(
            patch,
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(admin),
            section="security",
            summary="Cập nhật Cấu hình Dashboard (Step-up Auth & Presence Interval)",
            client_changes=payload.changes,
        )

        # Áp dụng ngay lập tức cho vòng lặp Discord Presence đang chạy (không cần restart container)
        applied_presence_live = False
        if apply_presence_fn is not None:
            updated_presence = updated.get("presence") if isinstance(updated.get("presence"), dict) else {}
            applied_presence_live = bool(await apply_presence_fn(updated_presence))

        return {
            "ok": True,
            "locked_sections": cleaned_sections,
            "step_up_cooldown_sec": cooldown_sec,
            "presence_interval_sec": interval_sec,
            "presence_auto_rotate": bool((updated.get("presence") or {}).get("auto_rotate", False)),
            "applied_presence_live": applied_presence_live,
            "sync": sync_coordinator.sync_status(),
        }

    async def _persist_tunnel_payload(
        payload: TunnelConfigPayload,
        user: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Xác thực payload Tunnel, lưu cấu hình vào MongoDB, ghi Audit Log và đồng bộ WARP nếu có."""
        current_cfg = await config_service.get_config(force=True)
        existing_tunnel = current_cfg.get("tunnel") if isinstance(current_cfg.get("tunnel"), dict) else {}
        existing_token = str(existing_tunnel.get("cf_tunnel_token") or "")
        existing_warp = bool(existing_tunnel.get("warp_outbound_enabled", True))

        try:
            clean_tunnel = sanitize_tunnel_config(
                cf_tunnel_enabled=payload.cf_tunnel_enabled,
                cf_tunnel_mode=payload.cf_tunnel_mode,
                cf_tunnel_token=payload.cf_tunnel_token,
                tunnel_only_inbound=payload.tunnel_only_inbound,
                proxy_headers=payload.proxy_headers,
                warp_outbound_enabled=payload.warp_outbound_enabled,
                existing_warp_enabled=existing_warp,
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

        if apply_warp_fn is not None and payload.warp_outbound_enabled is not None:
            await apply_warp_fn(clean_tunnel["warp_outbound_enabled"])

        return clean_tunnel, updated

    @router.put("/api/config/tunnel")
    async def api_update_tunnel_config(
        payload: TunnelConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Lưu cấu hình Cloudflare Tunnel vào MongoDB, ghi Audit Log và khởi động/tắt nóng tiến trình `cloudflared`."""
        clean_tunnel, updated = await _persist_tunnel_payload(payload, user)
        tunnel_status: Optional[dict[str, Any]] = None
        if apply_tunnel_fn is not None:
            tunnel_status = await apply_tunnel_fn(clean_tunnel)

        return {
            "ok": True,
            "config": _mask_config_secrets(updated),
            "cloudflare_tunnel": tunnel_status,
            "sync": sync_coordinator.sync_status(),
        }

    @router.post("/api/tunnel/update", status_code=202)
    async def api_tunnel_deferred_update(
        payload: TunnelConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Lưu cấu hình Tunnel, ghi Audit Log và trì hoãn restart `cloudflared` 0.8s trong nền (trả 202 Accepted tránh 502 qua Tunnel cũ)."""
        clean_tunnel, updated = await _persist_tunnel_payload(payload, user)
        tunnel_status: Optional[dict[str, Any]] = None
        if schedule_tunnel_fn is not None:
            tunnel_status = schedule_tunnel_fn(clean_tunnel)
        elif apply_tunnel_fn is not None:
            tunnel_status = await apply_tunnel_fn(clean_tunnel)

        return JSONResponse(
            status_code=202,
            content={
                "ok": True,
                "status": "restarting",
                "config": _mask_config_secrets(updated),
                "cloudflare_tunnel": tunnel_status,
                "sync": sync_coordinator.sync_status(),
            },
        )

    @router.get("/api/tunnel/status")
    async def api_get_tunnel_status(
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Trả về trạng thái thực tế của `cloudflared` (`restarting` / `connected` / `error` / `stopped`) để Dashboard xác minh sau khi cập nhật."""
        snap = get_tunnel_status_fn() if callable(get_tunnel_status_fn) else None
        if snap is None:
            return {"status": "stopped", "running": False, "edge_connected": False, "last_error": None}
        return snap

    async def _execute_warp_change(
        *,
        enabled: bool,
        port: Optional[int],
        force_reconnect: bool,
        user: dict[str, Any],
        summary: str,
        client_changes: Optional[dict[str, dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        actor, actor_type = auth_deps.actor_label(user)
        updated = await config_service.update_config(
            {"tunnel": {"warp_outbound_enabled": enabled}},
            updated_by=actor,
            actor_type=actor_type,
            actor_meta=auth_deps.actor_meta(user),
            section="tunnel",
            summary=summary,
            client_changes=client_changes,
        )

        warp_status: Optional[dict[str, Any]] = None
        if enabled:
            if force_reconnect and update_warp_fn is not None:
                warp_status = await update_warp_fn(port=port)
            elif start_warp_fn is not None:
                warp_status = await start_warp_fn(port=port)
            elif apply_warp_fn is not None:
                warp_status = await apply_warp_fn(True, port=port)
        else:
            if stop_warp_fn is not None:
                warp_status = await stop_warp_fn()
            elif apply_warp_fn is not None:
                warp_status = await apply_warp_fn(False, port=port)

        tunnel_status = get_tunnel_status_fn() if callable(get_tunnel_status_fn) else None
        return {
            "ok": True,
            "config": _mask_config_secrets(updated),
            "warp": warp_status,
            "cloudflare_tunnel": tunnel_status,
            "sync": sync_coordinator.sync_status(),
        }

    @router.put("/api/config/warp")
    async def api_update_warp_config(
        payload: WarpConfigPayload,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Bật/tắt nóng định tuyến Outbound (Gemini & Discord) qua Cloudflare WARP User-Space Proxy và poll socket loopback."""
        enabled = bool(payload.warp_outbound_enabled)
        summary = (
            "Bật định tuyến Outbound qua Cloudflare WARP (127.0.0.1)"
            if enabled
            else "Tắt định tuyến Outbound Cloudflare WARP (chuyển về Direct)"
        )
        return await _execute_warp_change(
            enabled=enabled,
            port=payload.warp_proxy_port,
            force_reconnect=False,
            user=user,
            summary=summary,
            client_changes=payload.changes,
        )

    @router.post("/api/warp/start")
    async def api_warp_start(
        payload: Optional[WarpActionPayload] = None,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Tạo hồ sơ `wgcf` (nếu thiếu), sinh `wireproxy.conf` bind `127.0.0.1:<port>`, khởi chạy `wireproxy` và poll socket trước khi trả trạng thái."""
        port = payload.warp_proxy_port if payload is not None else None
        changes = payload.changes if payload is not None else None
        return await _execute_warp_change(
            enabled=True,
            port=port,
            force_reconnect=False,
            user=user,
            summary="Khởi động & kết nối Cloudflare WARP User-Space Proxy",
            client_changes=changes,
        )

    @router.post("/api/warp/update")
    async def api_warp_update(
        payload: Optional[WarpActionPayload] = None,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Sinh lại `wireproxy.conf`, khởi động lại `wireproxy` và poll socket `127.0.0.1:<port>` tới 6–8 giây."""
        enabled = True if (payload is None or payload.warp_outbound_enabled is None) else bool(payload.warp_outbound_enabled)
        port = payload.warp_proxy_port if payload is not None else None
        changes = payload.changes if payload is not None else None
        summary = (
            "Cập nhật & kết nối lại Cloudflare WARP User-Space Proxy"
            if enabled
            else "Tắt định tuyến Outbound Cloudflare WARP (chuyển về Direct)"
        )
        return await _execute_warp_change(
            enabled=enabled,
            port=port,
            force_reconnect=enabled,
            user=user,
            summary=summary,
            client_changes=changes,
        )

    @router.post("/api/warp/stop")
    async def api_warp_stop(
        payload: Optional[WarpActionPayload] = None,
        user: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        """Tắt định tuyến Outbound Cloudflare WARP, dừng `wireproxy` và chuyển ngay về Direct."""
        changes = payload.changes if payload is not None else None
        return await _execute_warp_change(
            enabled=False,
            port=None,
            force_reconnect=False,
            user=user,
            summary="Tắt định tuyến Outbound Cloudflare WARP (chuyển về Direct)",
            client_changes=changes,
        )

    @router.api_route("/api/warp/trace", methods=["GET", "POST"])
    async def api_warp_trace(_user: dict[str, Any] = Depends(auth_deps.require_admin)):
        """Xác minh lưu lượng đi qua WARP bằng `cdn-cgi/trace` qua `socks5h://127.0.0.1:<port>` (chỉ trả `warp`/`colo`/`loc`, không lộ IP thô)."""
        if trace_warp_fn is None:
            return {"ok": False, "trace": None, "warp": None}
        result = await trace_warp_fn()
        trace = result.get("trace") if isinstance(result, dict) else None
        return {
            "ok": bool(trace and trace.get("warp_ok")),
            "trace": trace,
            "warp": result.get("warp") if isinstance(result, dict) else None,
        }

    return router
