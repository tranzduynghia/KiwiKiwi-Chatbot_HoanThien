"""Router FastAPI cho xác thực kép (Password + Discord OAuth2) và quản lý tài khoản Dashboard."""
from __future__ import annotations

import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.sync import WriteBehindSyncCoordinator
from app.features.auth.dependencies import (
    AuthDependencies,
    OAUTH_STATE_COOKIE_NAME,
    OAUTH_STATE_MAX_AGE,
)
from app.features.auth.oauth import DiscordOAuthError, DiscordOAuthService
from app.features.auth.schemas import ChangePasswordPayload, CreateUserPayload, LoginPayload
from app.features.auth.service import AuthService
from app.features.permissions.service import PermissionService


def _login_error_redirect(message: str) -> RedirectResponse:
    """Chuyển hướng về trang đăng nhập kèm thông báo lỗi trên query string."""
    return RedirectResponse(url="/login?" + urlencode({"error": message}), status_code=302)


def create_auth_router(
    auth_service: AuthService,
    oauth_service: DiscordOAuthService,
    permission_service: PermissionService,
    sync_coordinator: WriteBehindSyncCoordinator,
    auth_deps: AuthDependencies,
) -> APIRouter:
    """Tạo router cho `/auth/*`, `/api/me`, và `/api/permissions/users*`."""
    router = APIRouter(tags=["auth"])

    # ------------------------------------------------------------------
    # Xác thực kép: Username/Password + Discord OAuth2
    # ------------------------------------------------------------------
    @router.post("/auth/login")
    async def auth_login(payload: LoginPayload, request: Request):
        user = await auth_service.authenticate_password(payload.username, payload.password)
        if not user:
            raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu.")
        session_data = {
            "auth_type": "password",
            "username": user["username"],
            "display_name": user["username"],
            "role": user["role"],
            "is_admin": user["is_admin"],
            "login_at": int(time.time()),
        }
        response = JSONResponse({"ok": True, "user": session_data})
        auth_deps.set_session_cookie(response, request, session_data)
        return response

    @router.get("/auth/discord")
    async def auth_discord_start(request: Request):
        client_id, client_secret, redirect_uri = oauth_service.resolve_config(request)
        if not client_id or not client_secret:
            return _login_error_redirect("Chưa cấu hình DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET trong .env")

        state = secrets.token_urlsafe(24)
        authorize_url = oauth_service.build_authorize_url(client_id, redirect_uri, state)
        response = RedirectResponse(url=authorize_url, status_code=302)
        response.set_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            value=auth_deps.signer.encode({"state": state}),
            max_age=OAUTH_STATE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=auth_deps.is_secure_request(request),
        )
        return response

    @router.get("/auth/discord/callback")
    async def auth_discord_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        if error:
            return _login_error_redirect(f"Đăng nhập Discord bị hủy: {error}")
        if not code or not state:
            return _login_error_redirect("Thiếu tham số xác thực từ Discord OAuth2.")

        saved_payload = auth_deps.signer.decode(
            request.cookies.get(OAUTH_STATE_COOKIE_NAME),
            max_age=OAUTH_STATE_MAX_AGE,
        )
        saved_state = saved_payload.get("state") if saved_payload else None
        if not saved_state or not secrets.compare_digest(saved_state, state):
            return _login_error_redirect("Phiên OAuth2 không hợp lệ hoặc đã hết hạn. Vui lòng thử lại.")

        client_id, client_secret, redirect_uri = oauth_service.resolve_config(request)
        try:
            discord_user = await oauth_service.fetch_discord_user(
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
        except DiscordOAuthError as exc:
            return _login_error_redirect(str(exc))

        uid = str(discord_user.get("id", ""))
        allowed, is_admin = await permission_service.can_access_dashboard(uid)
        if not allowed:
            return _login_error_redirect(
                f"Discord ID {uid} không nằm trong danh sách được phép truy cập Dashboard."
            )

        avatar_hash = discord_user.get("avatar")
        avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png?size=64" if avatar_hash else ""
        session_data = {
            "auth_type": "discord",
            "discord_id": uid,
            "username": discord_user.get("username", uid),
            "display_name": discord_user.get("global_name") or discord_user.get("username") or uid,
            "avatar_url": avatar_url,
            "role": "admin" if is_admin else "operator",
            "is_admin": is_admin,
            "login_at": int(time.time()),
        }
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.delete_cookie(OAUTH_STATE_COOKIE_NAME)
        auth_deps.set_session_cookie(response, request, session_data)
        return response

    @router.api_route("/auth/logout", methods=["GET", "POST"])
    async def auth_logout():
        response = RedirectResponse(url="/login", status_code=302)
        auth_deps.clear_session_cookie(response)
        return response

    # ------------------------------------------------------------------
    # Thông tin phiên hiện tại & Quản trị tài khoản Dashboard
    # ------------------------------------------------------------------
    @router.get("/api/me")
    async def api_me(user: dict[str, Any] = Depends(auth_deps.require_user)):
        return {"user": user}

    @router.get("/api/permissions/users")
    async def api_list_users(user: dict[str, Any] = Depends(auth_deps.require_user)):
        return {
            "items": await auth_service.list_users(),
            "can_manage": bool(user.get("is_admin")),
        }

    @router.post("/api/permissions/users")
    async def api_create_user(
        payload: CreateUserPayload,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        try:
            actor, actor_type = auth_deps.actor_label(admin)
            created = await auth_service.create_user(
                payload.username,
                payload.password,
                role=payload.role,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
            return {"ok": True, "item": created, "sync": sync_coordinator.sync_status()}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/permissions/users/{username}/password")
    async def api_change_password(
        username: str,
        payload: ChangePasswordPayload,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        try:
            actor, actor_type = auth_deps.actor_label(admin)
            updated = await auth_service.change_password(
                username,
                payload.password,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
            return {"ok": True}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/api/permissions/users/{username}")
    async def api_delete_user(
        username: str,
        admin: dict[str, Any] = Depends(auth_deps.require_admin),
    ):
        try:
            actor, actor_type = auth_deps.actor_label(admin)
            deleted = await auth_service.delete_user(
                username,
                current_admin=admin,
                actor=actor,
                actor_type=actor_type,
                actor_meta=auth_deps.actor_meta(admin),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
        return {"ok": True}

    return router
