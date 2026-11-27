"""Router API quản trị nội dung Pháp lý Rich-Text 5 trang (đọc theo yêu cầu), danh sách chấp thuận ToS và Web Consent Gate."""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from urllib.parse import urlparse

from app.core.timezone import format_timezone_badge, resolve_request_timezone
from app.features.auth.dependencies import AuthDependencies
from app.features.legal.schemas import AcceptTosPayload, UpdateDiscordPromptPayload, UpdateLegalDocumentPayload
from app.features.legal.service import LegalService


def _validate_csrf_origin(request: Request) -> None:
    """Xác thực header `Origin` khớp với `Host` để chống giả mạo yêu cầu xuyên nguồn (CSRF)."""
    origin = (request.headers.get("origin") or "").strip()
    if not origin:
        return
    parsed = urlparse(origin)
    origin_host = (parsed.netloc or "").strip().lower()
    req_host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
    if origin_host and req_host and origin_host != req_host:
        raise HTTPException(status_code=403, detail="Origin không hợp lệ (bảo vệ CSRF).")


def _web_user_consent_key(user: dict[str, Any]) -> tuple[str, str]:
    """Xác định khóa định danh & tên hiển thị cho người dùng Web Dashboard khi kiểm tra/ghi nhận ToS."""
    discord_id = str(user.get("discord_id") or "").strip()
    if discord_id:
        display = str(user.get("display_name") or user.get("username") or discord_id)
        return discord_id, display
    username = str(user.get("username") or "admin").strip()
    return f"web:{username}", username


def create_legal_router(
    *,
    legal_service: LegalService,
    auth_deps: AuthDependencies,
) -> APIRouter:
    """Tạo các endpoint `/api/legal*` phục vụ tab Điều khoản & Pháp lý và Web Consent Gate."""
    router = APIRouter(prefix="/api/legal", tags=["legal"])

    @router.get("")
    async def api_get_legal(
        request: Request,
        doc_key: str = Query(default="toslinks"),
        tz: Optional[str] = Query(default=None),
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        tz_name, tz_info = resolve_request_timezone(request.headers, fallback_tz=tz)
        try:
            data = await legal_service.get_cms_overview(doc_key=doc_key, tz_info=tz_info)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            **data,
            "timezone": tz_name,
            "timezone_label": format_timezone_badge(tz_name, tz_info),
            "can_manage": bool(user.get("is_admin")),
        }

    @router.put("")
    async def api_update_legal(
        payload: UpdateLegalDocumentPayload,
        request: Request,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        _validate_csrf_origin(request)
        tz_name, tz_info = resolve_request_timezone(request.headers)
        actor, actor_type = auth_deps.actor_label(admin)
        try:
            await legal_service.update_document(
                doc_key=payload.doc_key,
                title=payload.title,
                subtitle=payload.subtitle,
                body=payload.body,
                active_tos_version=payload.active_tos_version,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
                client_changes=payload.changes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        data = await legal_service.get_cms_overview(doc_key=payload.doc_key, tz_info=tz_info)
        return {
            "ok": True,
            **data,
            "timezone": tz_name,
            "timezone_label": format_timezone_badge(tz_name, tz_info),
        }

    @router.put("/discord-prompt")
    async def api_update_discord_prompt(
        payload: UpdateDiscordPromptPayload,
        request: Request,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        _validate_csrf_origin(request)
        actor, actor_type = auth_deps.actor_label(admin)
        try:
            updated = await legal_service.update_discord_prompt_config(
                welcome_template=payload.discord_tos_welcome_template,
                update_template=payload.discord_tos_update_template,
                button_label=payload.discord_tos_button_label,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
                client_changes=payload.changes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "discord_prompt": updated,
        }

    @router.get("/acceptances")
    async def api_list_tos_acceptances(
        request: Request,
        limit: int = Query(default=15, ge=1, le=50),
        cursor: Optional[str] = Query(default=None),
        q: str = Query(default=""),
        tz: Optional[str] = Query(default=None),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        tz_name, tz_info = resolve_request_timezone(request.headers, fallback_tz=tz)
        result = await legal_service.list_acceptances(
            limit=limit,
            cursor=cursor,
            search=q,
            tz_info=tz_info,
        )
        return {
            **result,
            "timezone": tz_name,
            "timezone_label": format_timezone_badge(tz_name, tz_info),
        }

    @router.post("/acceptances/{discord_user_id}/revoke")
    async def api_revoke_tos_acceptance(
        discord_user_id: str,
        request: Request,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        _validate_csrf_origin(request)
        actor, actor_type = auth_deps.actor_label(admin)
        try:
            result = await legal_service.revoke_user_consent(
                discord_user_id=discord_user_id,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            **result,
        }

    @router.get("/consent-status")
    async def api_consent_status(user: dict[str, Any] = Depends(auth_deps.require_user)):
        uid, _ = _web_user_consent_key(user)
        status = await legal_service.get_user_consent_status(uid)
        return {
            **status,
            "requires_consent": not status["accepted"],
        }

    @router.post("/accept")
    async def api_accept_tos(
        payload: AcceptTosPayload,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        uid, display_name = _web_user_consent_key(user)
        doc = await legal_service.accept_current_tos(
            user_id=uid,
            username=display_name,
            source=payload.source or "web",
        )
        return {
            "ok": True,
            "accepted": True,
            "active_tos_version": doc["tos_accepted_version"],
            "consent_source": doc["consent_source"],
        }

    return router
