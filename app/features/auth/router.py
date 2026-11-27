"""Router FastAPI cho xác thực kép (Password + Discord OAuth2) và quản lý tài khoản Dashboard."""
from __future__ import annotations

import hmac
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
    STEP_UP_COOKIE_NAME,
    STEP_UP_IDLE_SECONDS,
)
from app.features.auth.login_history import AdminLoginHistoryRepository
from app.features.auth.oauth import DiscordOAuthError, DiscordOAuthService
from app.features.auth.schemas import (
    ChangePasswordPayload,
    CreateUserPayload,
    LoginPayload,
    StepUpPasswordPayload,
)
from app.features.auth.service import AuthService
from app.features.permissions.service import PermissionService

DISCORD_AVATAR_HASH_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
STEP_UP_TAB_MAP = {
    "perms": "perms",
    "admin": "perms",
    "permissions": "perms",
    "sessions": "sessions",
    "session": "sessions",
    "temp": "temp",
    "temp-page": "temp-page",
}


def _normalize_step_up_tab(raw_tab: Optional[str]) -> str:
    """Chuẩn hóa tên tab nhạy cảm đích cho luồng Discord OAuth2 Step-up (chống Open Redirect)."""
    cleaned = str(raw_tab or "").strip().lower().lstrip("#")
    return STEP_UP_TAB_MAP.get(cleaned, "perms")


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
    blacklist_repo: Any = None,
) -> APIRouter:
    """Tạo router cho `/auth/*`, `/api/me`, `/api/auth/login-history` và `/api/permissions/users*`."""
    router = APIRouter(tags=["auth"])
    login_ip_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=2048)
    login_user_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=1024)
    stepup_ip_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=1024)
    stepup_user_limiter = SlidingWindowRateLimiter(max_attempts=5, window_seconds=60.0, max_keys=1024)

    def _step_up_error_redirect(
        target_tab: str,
        error_code: str,
        *,
        request: Request,
        clear_oauth_state: bool = True,
    ) -> RedirectResponse:
        safe_tab = _normalize_step_up_tab(target_tab)
        safe_code = re.sub(r"[^a-z0-9_-]", "", str(error_code or "failed").lower())[:64] or "failed"
        if safe_tab == "temp-page":
            target_url = f"/temp?{urlencode({'step_up_error': safe_code})}"
        elif safe_tab == "temp":
            target_url = f"/dashboard?{urlencode({'step_up_error': safe_code})}#legal"
        else:
            target_url = f"/dashboard?{urlencode({'step_up_error': safe_code})}#{safe_tab}"
        response = RedirectResponse(
            url=target_url,
            status_code=302,
        )
        is_secure = auth_deps.is_secure_request(request)
        if clear_oauth_state:
            response.delete_cookie(
                key=OAUTH_STATE_COOKIE_NAME,
                path="/",
                httponly=True,
                samesite="lax",
                secure=is_secure,
            )
        auth_deps.clear_step_up_cookie(response, request)
        return response

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
            "session_id": secrets.token_urlsafe(16),
            "login_at": int(time.time()),
        }
        public_user = auth_deps.public_user_payload(cookie_session_data)
        response = JSONResponse({"ok": True, "user": public_user})
        auth_deps.set_session_cookie(response, request, cookie_session_data)
        auth_deps.clear_step_up_cookie(response, request)
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
        saved_payload = auth_deps.signer.decode(
            request.cookies.get(OAUTH_STATE_COOKIE_NAME),
            max_age=OAUTH_STATE_MAX_AGE,
        )

        # Luồng riêng cho Step-up Authentication bằng Discord OAuth2
        if isinstance(saved_payload, dict) and saved_payload.get("mode") == "step_up":
            target_tab = _normalize_step_up_tab(saved_payload.get("tab"))
            if error:
                return _step_up_error_redirect(target_tab, "cancelled", request=request)
            if not code or not state:
                return _step_up_error_redirect(target_tab, "missing_params", request=request)

            saved_state = saved_payload.get("state")
            if not saved_state or not secrets.compare_digest(str(saved_state), str(state)):
                return _step_up_error_redirect(target_tab, "invalid_state", request=request)

            decoded_state = auth_deps.signer.decode(state, max_age=OAUTH_STATE_MAX_AGE)
            if (
                not isinstance(decoded_state, dict)
                or decoded_state.get("mode") != "step_up"
                or not hmac.compare_digest(str(decoded_state.get("uid") or ""), str(saved_payload.get("uid") or ""))
                or not hmac.compare_digest(str(decoded_state.get("sid") or ""), str(saved_payload.get("sid") or ""))
            ):
                return _step_up_error_redirect(target_tab, "invalid_state", request=request)

            current_user = await auth_deps.get_current_user(request)
            current_uid = str(getattr(request.state, "auth_user_id", "") or "")
            current_sid = str(getattr(request.state, "auth_session_id", "") or "")
            expected_discord_id = str(saved_payload.get("uid") or "").strip()
            if (
                not current_user
                or current_user.get("auth_type") != "discord"
                or not expected_discord_id.isdigit()
                or not hmac.compare_digest(str(current_user.get("discord_id") or ""), expected_discord_id)
                or not hmac.compare_digest(current_sid, str(saved_payload.get("sid") or ""))
            ):
                return _step_up_error_redirect(target_tab, "session_mismatch", request=request)

            client_id, client_secret, redirect_uri = oauth_service.resolve_config(request)
            try:
                discord_user = await oauth_service.fetch_discord_user(
                    code=code,
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri,
                )
            except DiscordOAuthError:
                return _step_up_error_redirect(target_tab, "oauth_failed", request=request)

            returned_uid = str(discord_user.get("id", "")).strip()
            if not returned_uid.isdigit() or not hmac.compare_digest(returned_uid, expected_discord_id):
                return _step_up_error_redirect(target_tab, "discord_mismatch", request=request)

            allowed, _is_admin = await permission_service.can_access_dashboard(returned_uid)
            if not allowed:
                return _login_error_redirect(
                    f"Discord ID {returned_uid} không nằm trong danh sách được phép truy cập Dashboard.",
                    clear_oauth_state=True,
                    secure=is_secure,
                )

            if target_tab == "temp-page":
                ok_url = "/temp?step_up=ok"
            elif target_tab == "temp":
                ok_url = "/dashboard?step_up=ok#legal"
            else:
                ok_url = f"/dashboard?step_up=ok#{target_tab}"
            response = RedirectResponse(url=ok_url, status_code=302)
            response.delete_cookie(
                key=OAUTH_STATE_COOKIE_NAME,
                path="/",
                httponly=True,
                samesite="lax",
                secure=is_secure,
            )
            auth_deps.set_step_up_cookie(
                response,
                request,
                user_id=current_uid,
                session_id=current_sid,
                method="discord",
            )
            return response

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
            "session_id": secrets.token_urlsafe(16),
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
        auth_deps.clear_step_up_cookie(response, request)
        return response

    @router.post("/auth/logout")
    async def auth_logout(request: Request):
        response = RedirectResponse(url="/login", status_code=303)
        auth_deps.clear_session_cookie(response, request)
        auth_deps.clear_step_up_cookie(response, request)
        return response

    # ------------------------------------------------------------------
    # Step-up Authentication (Stateless 60s Sliding Idle Window)
    # ------------------------------------------------------------------
    @router.get("/api/auth/step-up/status")
    async def api_step_up_status(
        request: Request,
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Kiểm tra trạng thái Step-up hiện tại mà KHÔNG làm trượt `last_active_at`."""
        user_id = str(getattr(request.state, "auth_user_id", "") or "")
        session_id = str(getattr(request.state, "auth_session_id", "") or "")
        valid, remaining, _payload = auth_deps.inspect_step_up(
            request,
            user_id=user_id,
            session_id=session_id,
        )
        response = JSONResponse({
            "unlocked": valid,
            "remaining_seconds": remaining if valid else 0,
            "timeout_seconds": STEP_UP_IDLE_SECONDS,
        })
        if not valid and request.cookies.get(STEP_UP_COOKIE_NAME):
            auth_deps.clear_step_up_cookie(response, request)
        return response

    @router.post("/api/auth/step-up/password")
    async def api_step_up_password(
        payload: StepUpPasswordPayload,
        request: Request,
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Xác thực lại mật khẩu quản trị (có rate-limit) để cấp cookie `kiwikiwi_stepup` 60s."""
        client_ip = _extract_client_ip(request)
        user_id = str(getattr(request.state, "auth_user_id", "") or "")
        session_id = str(getattr(request.state, "auth_session_id", "") or "")

        ip_ok, ip_retry = stepup_ip_limiter.check(client_ip)
        user_ok, user_retry = stepup_user_limiter.check(user_id or client_ip)
        if not ip_ok or not user_ok:
            retry_after = max(ip_retry, user_retry, 1)
            return JSONResponse(
                {"detail": f"Quá nhiều lần xác thực sai. Vui lòng thử lại sau {retry_after} giây."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if user.get("auth_type") != "password":
            raise HTTPException(
                status_code=400,
                detail="Phiên hiện tại đăng nhập bằng Discord — vui lòng xác thực lại bằng Discord OAuth2.",
            )

        ok = await auth_service.verify_user_password(str(user.get("username") or ""), payload.password)
        if not ok:
            stepup_ip_limiter.record_failure(client_ip)
            stepup_user_limiter.record_failure(user_id or client_ip)
            raise HTTPException(status_code=401, detail="Mật khẩu xác thực không chính xác.")

        stepup_user_limiter.reset(user_id or client_ip)
        now_ts = int(time.time())
        response = JSONResponse({
            "ok": True,
            "expires_in": STEP_UP_IDLE_SECONDS,
            "last_active_at": now_ts,
        })
        auth_deps.set_step_up_cookie(
            response,
            request,
            user_id=user_id,
            session_id=session_id,
            method="password",
            now=now_ts,
        )
        return response

    @router.get("/api/auth/step-up/discord")
    async def api_step_up_discord(
        request: Request,
        tab: Optional[str] = Query(default="perms", max_length=32),
        user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Khởi tạo luồng xác thực lại qua Discord OAuth2 với state đã ký lưu target tab hash."""
        target_tab = _normalize_step_up_tab(tab)
        if user.get("auth_type") != "discord":
            return _step_up_error_redirect(target_tab, "not_discord", request=request, clear_oauth_state=False)

        expected_discord_id = str(user.get("discord_id") or "").strip()
        session_id = str(getattr(request.state, "auth_session_id", "") or "")
        if not expected_discord_id.isdigit() or not session_id:
            return _step_up_error_redirect(target_tab, "session_invalid", request=request, clear_oauth_state=False)

        client_id, client_secret, redirect_uri = oauth_service.resolve_config(request)
        if not client_id or not client_secret:
            return _step_up_error_redirect(target_tab, "oauth_not_configured", request=request, clear_oauth_state=False)

        nonce = secrets.token_urlsafe(16)
        signed_state = auth_deps.signer.encode({
            "mode": "step_up",
            "nonce": nonce,
            "uid": expected_discord_id,
            "sid": session_id,
            "tab": target_tab,
        })
        authorize_url = oauth_service.build_authorize_url(client_id, redirect_uri, signed_state)
        response = RedirectResponse(url=authorize_url, status_code=302)
        response.set_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            value=auth_deps.signer.encode({
                "mode": "step_up",
                "state": signed_state,
                "nonce": nonce,
                "uid": expected_discord_id,
                "sid": session_id,
                "tab": target_tab,
            }),
            max_age=OAUTH_STATE_MAX_AGE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=auth_deps.is_secure_request(request),
        )
        return response

    @router.post("/api/auth/step-up/lock")
    async def api_step_up_lock(
        request: Request,
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        """Khóa ngay lập tức các mục nhạy cảm bằng cách xóa cookie `kiwikiwi_stepup`."""
        response = JSONResponse({"ok": True, "locked": True})
        auth_deps.clear_step_up_cookie(response, request)
        return response

    # ------------------------------------------------------------------
    # Thông tin phiên hiện tại, Lịch sử đăng nhập & Quản trị tài khoản Dashboard
    # ------------------------------------------------------------------
    @router.get("/api/me")
    async def api_me(user: dict[str, Any] = Depends(auth_deps.require_user)):
        discord_id = str(user.get("discord_id") or "").strip()
        if blacklist_repo is not None and discord_id:
            ban_reason = blacklist_repo.get_reason_fast(discord_id)
            if ban_reason is not None:
                raise HTTPException(
                    status_code=403,
                    detail=f"Tài khoản của bạn đã bị hạn chế sử dụng KiwiKiwi Bot. Lý do: {ban_reason}",
                )
        return {"user": auth_deps.public_user_payload(user)}

    @router.get("/api/auth/login-history")
    async def api_login_history(
        request: Request,
        limit: int = Query(default=15, ge=1, le=50),
        cursor: Optional[str] = Query(default=None),
        status: str = Query(default="all"),
        tz: Optional[str] = Query(default=None),
        _user: dict[str, Any] = Depends(auth_deps.require_step_up_auth),
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
    async def api_list_users(user: dict[str, Any] = Depends(auth_deps.require_step_up_auth)):
        return {
            "items": await auth_service.list_users(),
            "can_manage": bool(user.get("is_admin")),
        }

    @router.post("/api/permissions/users")
    async def api_create_user(
        payload: CreateUserPayload,
        admin: dict[str, Any] = Depends(auth_deps.require_admin_step_up),
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
        admin: dict[str, Any] = Depends(auth_deps.require_admin_step_up),
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
        admin: dict[str, Any] = Depends(auth_deps.require_admin_step_up),
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
