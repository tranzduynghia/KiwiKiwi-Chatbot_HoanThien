"""Router FastAPI cho xác thực kép (Password + Discord OAuth2) và quản lý tài khoản Dashboard."""
from __future__ import annotations

import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.security import SlidingWindowRateLimiter, is_trusted_proxy_ip
from app.core.sync import WriteBehindSyncCoordinator
from app.core.timezone import extract_cf_country, format_timezone_badge, resolve_request_timezone
from app.features.auth.dependencies import (
    AuthDependencies,
    OAUTH_STATE_COOKIE_NAME,
    OAUTH_STATE_MAX_AGE,
)
from app.features.auth.login_history import AdminLoginHistoryRepository
from app.features.auth.oauth import DiscordOAuthError, DiscordOAuthService
from app.features.auth.schemas import ChangePasswordPayload, CreateUserPayload, LoginPayload
from app.features.auth.service import AuthService
from app.features.permissions.service import PermissionService

DISCORD_AVATAR_HASH_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def _extract_client_ip(request: Request) -> str:
    """Trích xuất IP client an toàn (chỉ tin `CF-Connecting-IP`/`X-Forwarded-For` khi đến từ reverse proxy tin cậy)."""
    peer_host = request.client.host if request.client else "unknown"
    if is_trusted_proxy_ip(peer_host):
        cf_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf_ip:
            return cf_ip[:64]
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff[:64]
    return str(peer_host)[:64]


def _login_error_redirect(
    message: str,
    *,
    clear_oauth_state: bool = False,
    secure: bool = False,
) -> RedirectResponse:
    """Chuyển hướng về trang đăng nhập kèm thông báo lỗi rút gọn trên query string (và dọn cookie OAuth state nếu cần)."""
    clean_msg = " ".join(str(message or "").split()).strip()[:200]
    response = RedirectResponse(url="/login?" + urlencode({"error": clean_msg}), status_code=302)
    if clear_oauth_state:
        response.delete_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )
    return response


def create_auth_router(
    auth_service: AuthService,
    oauth_service: DiscordOAuthService,
    permission_service: PermissionService,
    sync_coordinator: WriteBehindSyncCoordinator,
    auth_deps: AuthDependencies,
    login_history_repo: Optional[AdminLoginHistoryRepository] = None,
) -> APIRouter:
    """Tạo router cho `/auth/*`, `/api/me`, `/api/auth/login-history` và `/api/permissions/users*`."""
    router = APIRouter(tags=["auth"])
    login_ip_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=2048)
    login_user_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=1024)

    async def _record_login_event(
        request: Request,
        *,
        username: str,
        status: str,
        auth_method: str,
        failure_reason: Optional[str] = None,
    ) -> None:
        if login_history_repo is None:
            return
        tz_name, _ = resolve_request_timezone(request.headers)
        await login_history_repo.record_attempt(
            username=username,
            status=status,
            failure_reason=failure_reason,
            auth_method=auth_method,
            ip_address=_extract_client_ip(request),
            country_code=extract_cf_country(request.headers),
            client_timezone=tz_name,
            user_agent=request.headers.get("user-agent") or "",
        )

    # ------------------------------------------------------------------
    # Xác thực kép: Username/Password + Discord OAuth2
    # ------------------------------------------------------------------
    @router.post("/auth/login")
    async def auth_login(payload: LoginPayload, request: Request):
        client_ip = _extract_client_ip(request)
        uname_key = payload.username.strip().lower()

        ip_ok, ip_retry = login_ip_limiter.check(client_ip)
        user_ok, user_retry = login_user_limiter.check(uname_key)
        if not ip_ok or not user_ok:
            retry_after = max(ip_retry, user_retry, 1)
            await _record_login_event(
                request,
                username=uname_key or "unknown",
                status="failed",
                auth_method="password",
                failure_reason="rate_limited",
            )
            return JSONResponse(
                {"detail": f"Quá nhiều lần đăng nhập sai. Vui lòng thử lại sau {retry_after} giây."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        user = await auth_service.authenticate_password(payload.username, payload.password)
        if not user:
            login_ip_limiter.record_failure(client_ip)
            login_user_limiter.record_failure(uname_key)
            await _record_login_event(
                request,
                username=uname_key or "unknown",
                status="failed",
                auth_method="password",
                failure_reason="invalid_credentials",
            )
            raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu.")

        login_user_limiter.reset(uname_key)
        await _record_login_event(
            request,
            username=user["username"],
            status="success",
            auth_method="password",
        )
        cookie_session_data = {
            "auth_type": "password",
            "username": user["username"],
            "display_name": user["username"],
            "role": user["role"],
            "is_admin": user["is_admin"],
            "session_version": user.get("session_version", ""),
            "login_at": int(time.time()),
        }
        public_user = auth_deps.public_user_payload(cookie_session_data)
        response = JSONResponse({"ok": True, "user": public_user})
        auth_deps.set_session_cookie(response, request, cookie_session_data)
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
            path="/",
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
        is_secure = auth_deps.is_secure_request(request)
        if error:
            safe_err = re.sub(r"[^A-Za-z0-9_ -]", "", str(error))[:64] or "access_denied"
            await _record_login_event(
                request,
                username="discord-oauth",
                status="failed",
                auth_method="discord_oauth",
                failure_reason=f"oauth_{safe_err}",
            )
            return _login_error_redirect(
                f"Đăng nhập Discord bị hủy: {safe_err}",
                clear_oauth_state=True,
                secure=is_secure,
            )
        if not code or not state:
            await _record_login_event(
                request,
                username="discord-oauth",
                status="failed",
                auth_method="discord_oauth",
                failure_reason="missing_oauth_params",
            )
            return _login_error_redirect(
                "Thiếu tham số xác thực từ Discord OAuth2.",
                clear_oauth_state=True,
                secure=is_secure,
            )

        saved_payload = auth_deps.signer.decode(
            request.cookies.get(OAUTH_STATE_COOKIE_NAME),
            max_age=OAUTH_STATE_MAX_AGE,
        )
        saved_state = saved_payload.get("state") if saved_payload else None
        if not saved_state or not secrets.compare_digest(str(saved_state), str(state)):
            await _record_login_event(
                request,
                username="discord-oauth",
                status="failed",
                auth_method="discord_oauth",
                failure_reason="invalid_oauth_state",
            )
            return _login_error_redirect(
                "Phiên OAuth2 không hợp lệ hoặc đã hết hạn. Vui lòng thử lại.",
                clear_oauth_state=True,
                secure=is_secure,
            )

        client_id, client_secret, redirect_uri = oauth_service.resolve_config(request)
        try:
            discord_user = await oauth_service.fetch_discord_user(
                code=code,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
        except DiscordOAuthError as exc:
            await _record_login_event(
                request,
                username="discord-oauth",
                status="failed",
                auth_method="discord_oauth",
                failure_reason="oauth_exchange_failed",
            )
            return _login_error_redirect(str(exc), clear_oauth_state=True, secure=is_secure)

        uid = str(discord_user.get("id", "")).strip()
        if not uid.isdigit():
            await _record_login_event(
                request,
                username="discord-oauth",
                status="failed",
                auth_method="discord_oauth",
                failure_reason="invalid_discord_id",
            )
            return _login_error_redirect(
                "Không xác thực được Discord User ID hợp lệ.",
                clear_oauth_state=True,
                secure=is_secure,
            )

        uname_label = str(discord_user.get("username") or uid)[:64]
        allowed, is_admin = await permission_service.can_access_dashboard(uid)
        if not allowed:
            await _record_login_event(
                request,
                username=f"{uname_label} ({uid})",
                status="failed",
                auth_method="discord_oauth",
                failure_reason="not_authorized",
            )
            return _login_error_redirect(
                f"Discord ID {uid} không nằm trong danh sách được phép truy cập Dashboard.",
                clear_oauth_state=True,
                secure=is_secure,
            )

        await _record_login_event(
            request,
            username=f"{uname_label} ({uid})",
            status="success",
            auth_method="discord_oauth",
        )
        raw_avatar = str(discord_user.get("avatar") or "").strip()
        avatar_url = (
            f"https://cdn.discordapp.com/avatars/{uid}/{raw_avatar}.png?size=64"
            if raw_avatar and DISCORD_AVATAR_HASH_RE.fullmatch(raw_avatar)
            else ""
        )
        session_data = {
            "auth_type": "discord",
            "discord_id": uid,
            "username": uname_label,
            "display_name": str(discord_user.get("global_name") or discord_user.get("username") or uid)[:64],
            "avatar_url": avatar_url,
            "role": "admin" if is_admin else "operator",
            "is_admin": is_admin,
            "login_at": int(time.time()),
        }
        response = RedirectResponse(url="/dashboard", status_code=302)
        response.delete_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="lax",
            secure=is_secure,
        )
        auth_deps.set_session_cookie(response, request, session_data)
        return response

    @router.post("/auth/logout")
    async def auth_logout(request: Request):
        response = RedirectResponse(url="/login", status_code=303)
        auth_deps.clear_session_cookie(response, request)
        return response

    # ------------------------------------------------------------------
    # Thông tin phiên hiện tại, Lịch sử đăng nhập & Quản trị tài khoản Dashboard
    # ------------------------------------------------------------------
    @router.get("/api/me")
    async def api_me(user: dict[str, Any] = Depends(auth_deps.require_user)):
        return {"user": auth_deps.public_user_payload(user)}

    @router.get("/api/auth/login-history")
    async def api_login_history(
        request: Request,
        limit: int = Query(default=15, ge=1, le=50),
        cursor: Optional[str] = Query(default=None),
        status: str = Query(default="all"),
        tz: Optional[str] = Query(default=None),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        tz_name, tz_info = resolve_request_timezone(request.headers, fallback_tz=tz)
        if login_history_repo is None:
            return {
                "items": [],
                "total": 0,
                "limit": limit,
                "status_filter": status,
                "cursor": "0",
                "next_cursor": None,
                "prev_cursor": None,
                "has_more": False,
                "timezone": tz_name,
                "timezone_label": format_timezone_badge(tz_name, tz_info),
            }
        result = await login_history_repo.list_history(
            limit=limit,
            cursor=cursor,
            status_filter=status,
            tz_info=tz_info,
        )
        return {
            **result,
            "timezone": tz_name,
            "timezone_label": format_timezone_badge(tz_name, tz_info),
        }

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
