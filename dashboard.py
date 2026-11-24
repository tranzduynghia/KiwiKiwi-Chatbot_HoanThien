"""
Web Dashboard tối giản (FastAPI + Jinja2 + Tailwind CDN) tích hợp chung tiến trình bot Discord.
- Xác thực kép: Tài khoản/Mật khẩu (MongoDB Atlas) + Login with Discord (OAuth2)
- Phân quyền: Quản lý Discord User ID (Admin / Dashboard / /status) và tài khoản Dashboard
- Giám sát bot theo thời gian thực + cấu hình /ask, System Instruction, Model Gemini lưu thẳng MongoDB Atlas
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from config_store import ConfigStore, DEFAULT_OCR_INSTRUCTION, DEFAULT_SYSTEM_PROMPT

log = logging.getLogger("gemini-bot.dashboard")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

SESSION_COOKIE_NAME = "kiwikiwi_session"
OAUTH_STATE_COOKIE_NAME = "kiwikiwi_oauth_state"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 ngày

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


# --------------------------------------------------------------------------
# Pydantic schemas cho API
# --------------------------------------------------------------------------
class LoginPayload(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class AskConfigPayload(BaseModel):
    ask: dict[str, Any]
    session: dict[str, Any]


class PromptConfigPayload(BaseModel):
    system_prompt: str = Field(..., min_length=1, max_length=20000)
    ocr_instruction: str = Field(..., min_length=1, max_length=10000)
    append_current_date: bool = True


class ModelsConfigPayload(BaseModel):
    default_text_model: str = ""
    auto_model_chain: list[str] = Field(default_factory=list)
    auto_last_model: str = "gemini-4-hydrogen"
    fallback_text: list[str] = Field(default_factory=list)
    hidden_models: list[str] = Field(default_factory=list)
    hide_pro_models: bool = True
    extra_models: list[str] = Field(default_factory=list)
    model_refresh_hours: float = 6.0
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DiscordPermPayload(BaseModel):
    user_id: str = Field(..., min_length=5, max_length=30)
    label: str = Field(default="", max_length=80)
    is_admin: bool = False
    can_dashboard: bool = False
    can_status: bool = False


class CreateUserPayload(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(default="admin")


class ChangePasswordPayload(BaseModel):
    password: str = Field(..., min_length=6, max_length=128)


# --------------------------------------------------------------------------
# Factory tạo FastAPI app gắn trực tiếp vào runtime của Bot
# --------------------------------------------------------------------------
def _render_template(templates: Jinja2Templates, request: Request, name: str, context: dict[str, Any]):
    """Tương thích cả Starlette/FastAPI cũ và mới."""
    try:
        return templates.TemplateResponse(request=request, name=name, context=context)
    except TypeError:
        return templates.TemplateResponse(name, {"request": request, **context})


def create_dashboard_app(
    *,
    config_store: ConfigStore,
    get_status_snapshot,
    get_models_snapshot,
    refresh_models_fn,
    clear_cooldowns_fn,
    is_bot_ready_fn,
    get_uptime_fn,
) -> FastAPI:
    app = FastAPI(title="KiwiKiwi Bot Dashboard", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    session_secret = (os.getenv("SESSION_SECRET") or "").strip()
    if not session_secret:
        session_secret = secrets.token_hex(32)
        log.warning(
            "Chưa đặt biến môi trường SESSION_SECRET — đang dùng khóa ngẫu nhiên tạm thời "
            "(phiên đăng nhập sẽ bị đăng xuất mỗi khi restart)."
        )
    serializer = URLSafeTimedSerializer(session_secret, salt="kiwikiwi-dashboard-v1")

    def _discord_oauth_config(request: Request) -> tuple[str, str, str]:
        client_id = (os.getenv("DISCORD_CLIENT_ID") or "").strip()
        client_secret = (os.getenv("DISCORD_CLIENT_SECRET") or "").strip()
        redirect_uri = (os.getenv("DISCORD_REDIRECT_URI") or "").strip()
        if not redirect_uri:
            public_url = (os.getenv("DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
            if public_url:
                redirect_uri = f"{public_url}/auth/discord/callback"
            else:
                scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
                host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
                redirect_uri = f"{scheme}://{host}/auth/discord/callback"
        return client_id, client_secret, redirect_uri

    def _is_secure_request(request: Request) -> bool:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        public_url = (os.getenv("DASHBOARD_PUBLIC_URL") or "").lower()
        return proto == "https" or public_url.startswith("https://")

    def _encode_session(data: dict[str, Any]) -> str:
        return serializer.dumps(data)

    def _decode_session(token: Optional[str]) -> Optional[dict[str, Any]]:
        if not token:
            return None
        try:
            data = serializer.loads(token, max_age=SESSION_MAX_AGE)
            return data if isinstance(data, dict) else None
        except (BadSignature, SignatureExpired):
            return None

    def _actor_label(user: dict[str, Any]) -> tuple[str, str]:
        if user.get("auth_type") == "discord":
            return f"{user.get('display_name') or user.get('username')} (discord:{user.get('discord_id')})", "discord"
        return str(user.get("username") or "dashboard"), "password"

    async def get_current_user(request: Request) -> Optional[dict[str, Any]]:
        sess = _decode_session(request.cookies.get(SESSION_COOKIE_NAME))
        if not sess:
            return None
        # Nếu đăng nhập bằng Discord OAuth2 -> kiểm tra lại quyền trực tiếp từ MongoDB theo thời gian thực
        if sess.get("auth_type") == "discord":
            uid = sess.get("discord_id")
            if not uid:
                return None
            allowed, is_admin = await config_store.can_access_dashboard_discord(uid)
            if not allowed:
                return None
            sess["is_admin"] = is_admin
            return sess
        # Nếu đăng nhập bằng tài khoản mật khẩu
        if sess.get("auth_type") == "password" and sess.get("username"):
            return sess
        return None

    async def require_user(request: Request) -> dict[str, Any]:
        user = await get_current_user(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Chưa đăng nhập hoặc phiên đã hết hạn.")
        return user

    async def require_admin(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if not user.get("is_admin"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Chỉ tài khoản Admin mới có quyền thực hiện thao tác này.")
        return user

    # ------------------------------------------------------------------
    # Health Check (giữ tương thích Northflank / Render / UptimeRobot)
    # ------------------------------------------------------------------
    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        return JSONResponse({
            "status": "ok",
            "discord_ready": bool(is_bot_ready_fn()),
            "uptime_seconds": int(get_uptime_fn()),
        })

    @app.api_route("/", methods=["GET", "HEAD"])
    async def root(request: Request):
        if request.method == "HEAD":
            return JSONResponse({"status": "ok"})
        user = await get_current_user(request)
        return RedirectResponse(url="/dashboard" if user else "/login", status_code=302)

    # ------------------------------------------------------------------
    # Giao diện HTML
    # ------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, error: Optional[str] = None):
        user = await get_current_user(request)
        if user:
            return RedirectResponse(url="/dashboard", status_code=302)
        client_id, client_secret, _ = _discord_oauth_config(request)
        return _render_template(
            templates,
            request,
            "login.html",
            {
                "error": error,
                "discord_oauth_enabled": bool(client_id and client_secret),
                "db_connected": config_store.connected,
            },
        )

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        user = await get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)
        return _render_template(
            templates,
            request,
            "dashboard.html",
            {
                "user": user,
                "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
                "default_ocr_instruction": DEFAULT_OCR_INSTRUCTION,
            },
        )

    # ------------------------------------------------------------------
    # Xác thực kép: Password + Discord OAuth2
    # ------------------------------------------------------------------
    @app.post("/auth/login")
    async def auth_login(payload: LoginPayload, request: Request):
        user = await config_store.authenticate_user(payload.username, payload.password)
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
        resp = JSONResponse({"ok": True, "user": session_data})
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=_encode_session(session_data),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return resp

    @app.get("/auth/discord")
    async def auth_discord_start(request: Request):
        client_id, client_secret, redirect_uri = _discord_oauth_config(request)
        if not client_id or not client_secret:
            return RedirectResponse(
                url="/login?" + urlencode({"error": "Chưa cấu hình DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET trong .env"}),
                status_code=302,
            )
        state = secrets.token_urlsafe(24)
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
        })
        resp = RedirectResponse(url=f"{DISCORD_AUTHORIZE_URL}?{params}", status_code=302)
        resp.set_cookie(
            key=OAUTH_STATE_COOKIE_NAME,
            value=serializer.dumps({"state": state}),
            max_age=300,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return resp

    @app.get("/auth/discord/callback")
    async def auth_discord_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        if error:
            return RedirectResponse(
                url="/login?" + urlencode({"error": f"Đăng nhập Discord bị hủy: {error}"}),
                status_code=302,
            )
        if not code or not state:
            return RedirectResponse(
                url="/login?" + urlencode({"error": "Thiếu tham số xác thực từ Discord OAuth2."}),
                status_code=302,
            )

        raw_state = request.cookies.get(OAUTH_STATE_COOKIE_NAME)
        try:
            saved_state = serializer.loads(raw_state or "", max_age=300).get("state")
        except (BadSignature, SignatureExpired):
            saved_state = None
        if not saved_state or not secrets.compare_digest(saved_state, state):
            return RedirectResponse(
                url="/login?" + urlencode({"error": "Phiên OAuth2 không hợp lệ hoặc đã hết hạn. Vui lòng thử lại."}),
                status_code=302,
            )

        client_id, client_secret, redirect_uri = _discord_oauth_config(request)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                token_res = await client.post(
                    DISCORD_TOKEN_URL,
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if token_res.status_code != 200:
                    log.warning("Discord OAuth2 token lỗi %s: %s", token_res.status_code, token_res.text[:200])
                    return RedirectResponse(
                        url="/login?" + urlencode({"error": "Không đổi được mã xác thực với Discord (kiểm tra Redirect URI / Client Secret)."}),
                        status_code=302,
                    )
                access_token = token_res.json().get("access_token")
                user_res = await client.get(
                    DISCORD_USER_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if user_res.status_code != 200:
                    return RedirectResponse(
                        url="/login?" + urlencode({"error": "Không lấy được thông tin tài khoản Discord."}),
                        status_code=302,
                    )
                discord_user = user_res.json()
        except Exception as e:
            log.warning("Lỗi gọi Discord OAuth2: %s", e)
            return RedirectResponse(
                url="/login?" + urlencode({"error": f"Lỗi kết nối Discord OAuth2: {type(e).__name__}"}),
                status_code=302,
            )

        uid = str(discord_user.get("id", ""))
        allowed, is_admin = await config_store.can_access_dashboard_discord(uid)
        if not allowed:
            return RedirectResponse(
                url="/login?" + urlencode({
                    "error": f"Discord ID {uid} không nằm trong danh sách được phép truy cập Dashboard."
                }),
                status_code=302,
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
        resp = RedirectResponse(url="/dashboard", status_code=302)
        resp.delete_cookie(OAUTH_STATE_COOKIE_NAME)
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=_encode_session(session_data),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return resp

    @app.api_route("/auth/logout", methods=["GET", "POST"])
    async def auth_logout():
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE_NAME)
        return resp

    # ------------------------------------------------------------------
    # API giám sát & cấu hình (yêu cầu đăng nhập)
    # ------------------------------------------------------------------
    @app.get("/api/me")
    async def api_me(user: dict[str, Any] = Depends(require_user)):
        return {"user": user}

    @app.get("/api/status")
    async def api_status(
        check_gemini: bool = Query(default=False),
        user: dict[str, Any] = Depends(require_user),
    ):
        return await get_status_snapshot(check_gemini=check_gemini)

    @app.post("/api/models/refresh")
    async def api_refresh_models(user: dict[str, Any] = Depends(require_user)):
        await refresh_models_fn()
        return {"ok": True, "models": await get_models_snapshot()}

    @app.post("/api/models/clear-cooldowns")
    async def api_clear_cooldowns(user: dict[str, Any] = Depends(require_user)):
        cleared = clear_cooldowns_fn()
        return {"ok": True, "cleared": cleared}

    @app.get("/api/config")
    async def api_get_config(user: dict[str, Any] = Depends(require_user)):
        cfg = await config_store.get_config(force=True)
        models_info = await get_models_snapshot()
        return {"config": cfg, "models_runtime": models_info}

    @app.put("/api/config/ask")
    async def api_update_ask_config(payload: AskConfigPayload, user: dict[str, Any] = Depends(require_user)):
        ask_in = payload.ask or {}
        sess_in = payload.session or {}

        # Chuẩn hóa và kiểm tra biên giá trị
        thinking_lv = str(ask_in.get("default_thinking_level") or "").strip().lower()
        if thinking_lv not in ("", "auto", "low", "medium", "high", "xhigh"):
            raise HTTPException(status_code=400, detail="default_thinking_level không hợp lệ.")

        budgets_in = ask_in.get("thinking_budgets") or {}
        clean_ask = {
            "user_cooldown": max(0.0, float(ask_in.get("user_cooldown", 5.0))),
            "max_concurrent": max(1, min(50, int(ask_in.get("max_concurrent", 2)))),
            "max_file_mb": max(1.0, min(50.0, float(ask_in.get("max_file_mb", 10.0)))),
            "allow_file": bool(ask_in.get("allow_file", True)),
            "allow_ocr": bool(ask_in.get("allow_ocr", True)),
            "allow_web_search": bool(ask_in.get("allow_web_search", True)),
            "allow_thinking": bool(ask_in.get("allow_thinking", True)),
            "default_web_search": bool(ask_in.get("default_web_search", False)),
            "default_thinking_level": thinking_lv,
            "thinking_budgets": {
                "low": max(128, int(budgets_in.get("low", 1024))),
                "medium": max(256, int(budgets_in.get("medium", 8192))),
                "high": max(512, int(budgets_in.get("high", 24576))),
            },
            "gemini_timeout_seconds": max(15.0, min(600.0, float(ask_in.get("gemini_timeout_seconds", 170.0)))),
            "max_retries": max(1, min(10, int(ask_in.get("max_retries", 3)))),
            "chunk_char_limit": max(500, min(1950, int(ask_in.get("chunk_char_limit", 1900)))),
            "max_reply_chunks": max(1, min(20, int(ask_in.get("max_reply_chunks", 6)))),
            "show_thoughts_embed": bool(ask_in.get("show_thoughts_embed", True)),
            "show_sources": bool(ask_in.get("show_sources", True)),
        }

        raw_ram = sess_in.get("ram_limit_mb")
        ram_limit = None
        if raw_ram not in (None, "", 0, "0"):
            ram_limit = max(32, int(raw_ram))

        clean_session = {
            "session_idle_hours": max(0.1, float(sess_in.get("session_idle_hours", 2.0))),
            "max_history_messages": max(2, min(200, int(sess_in.get("max_history_messages", 20)))),
            "max_history_chars": max(1000, int(sess_in.get("max_history_chars", 24000))),
            "max_stored_chars": max(500, int(sess_in.get("max_stored_chars", 4000))),
            "session_max_turns": max(1, int(sess_in.get("session_max_turns", 500))),
            "sweep_minutes": max(1.0, float(sess_in.get("sweep_minutes", 5.0))),
            "mongodb_quota_mb": max(1.0, float(sess_in.get("mongodb_quota_mb", 512.0))),
            "ram_limit_mb": ram_limit,
        }

        actor, actor_type = _actor_label(user)
        updated = await config_store.update_config(
            {"ask": clean_ask, "session": clean_session},
            updated_by=actor,
            actor_type=actor_type,
            section="ask",
            summary="Cập nhật cấu hình /ask & phiên chat",
        )
        return {"ok": True, "config": updated, "sync": config_store.sync_status()}

    @app.put("/api/config/prompt")
    async def api_update_prompt_config(payload: PromptConfigPayload, user: dict[str, Any] = Depends(require_user)):
        actor, actor_type = _actor_label(user)
        updated = await config_store.update_config(
            {
                "system_prompt": payload.system_prompt.strip(),
                "ocr_instruction": payload.ocr_instruction.strip(),
                "append_current_date": bool(payload.append_current_date),
            },
            updated_by=actor,
            actor_type=actor_type,
            section="prompt",
            summary="Cập nhật System Instruction / OCR Instruction",
        )
        return {"ok": True, "config": updated, "sync": config_store.sync_status()}

    @app.put("/api/config/models")
    async def api_update_models_config(payload: ModelsConfigPayload, user: dict[str, Any] = Depends(require_user)):
        clean_overrides: dict[str, dict[str, Any]] = {}
        for mid, ov in (payload.overrides or {}).items():
            mid_s = str(mid).strip()
            if not mid_s or not isinstance(ov, dict):
                continue
            entry: dict[str, Any] = {}
            if "display_name" in ov and str(ov["display_name"]).strip():
                entry["display_name"] = str(ov["display_name"]).strip()[:80]
            if "tag" in ov and str(ov["tag"]).strip():
                entry["tag"] = str(ov["tag"]).strip()[:16]
            if "is_free" in ov and ov["is_free"] is not None:
                entry["is_free"] = bool(ov["is_free"])
            if "hidden" in ov and ov["hidden"] is not None:
                entry["hidden"] = bool(ov["hidden"])
            if entry:
                clean_overrides[mid_s] = entry

        clean_models = {
            "default_text_model": payload.default_text_model.strip(),
            "auto_model_chain": [x.strip() for x in payload.auto_model_chain if x and x.strip()],
            "auto_last_model": payload.auto_last_model.strip(),
            "fallback_text": [x.strip() for x in payload.fallback_text if x and x.strip()]
            or ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"],
            "hidden_models": [x.strip() for x in payload.hidden_models if x and x.strip()],
            "hide_pro_models": bool(payload.hide_pro_models),
            "extra_models": [x.strip() for x in payload.extra_models if x and x.strip()],
            "model_refresh_hours": max(0.25, float(payload.model_refresh_hours)),
            "overrides": clean_overrides,
        }

        actor, actor_type = _actor_label(user)
        updated = await config_store.update_config(
            {"models": clean_models},
            updated_by=actor,
            actor_type=actor_type,
            section="models",
            summary="Cập nhật cấu hình Model Gemini",
        )
        return {"ok": True, "config": updated, "models_runtime": await get_models_snapshot(), "sync": config_store.sync_status()}

    # ------------------------------------------------------------------
    # API Phân quyền Discord & Tài khoản Dashboard (chỉ Admin)
    # ------------------------------------------------------------------
    @app.get("/api/permissions/discord")
    async def api_list_discord_perms(user: dict[str, Any] = Depends(require_user)):
        return {"items": await config_store.list_discord_permissions(), "can_manage": bool(user.get("is_admin"))}

    @app.post("/api/permissions/discord")
    async def api_upsert_discord_perm(payload: DiscordPermPayload, admin: dict[str, Any] = Depends(require_admin)):
        try:
            actor, actor_type = _actor_label(admin)
            doc = await config_store.upsert_discord_permission(
                payload.user_id,
                label=payload.label,
                is_admin=payload.is_admin,
                can_dashboard=payload.can_dashboard,
                can_status=payload.can_status,
                actor=actor,
                actor_type=actor_type,
            )
            return {"ok": True, "item": doc, "sync": config_store.sync_status()}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/permissions/discord/{user_id}")
    async def api_delete_discord_perm(user_id: str, admin: dict[str, Any] = Depends(require_admin)):
        if admin.get("auth_type") == "discord" and str(admin.get("discord_id")) == str(user_id):
            raise HTTPException(status_code=400, detail="Không thể tự xóa quyền của chính bạn đang đăng nhập.")
        actor, actor_type = _actor_label(admin)
        deleted = await config_store.delete_discord_permission(user_id, actor=actor, actor_type=actor_type)
        if not deleted:
            raise HTTPException(status_code=404, detail="Không tìm thấy Discord User ID này.")
        return {"ok": True}

    @app.get("/api/permissions/users")
    async def api_list_users(user: dict[str, Any] = Depends(require_user)):
        return {"items": await config_store.list_dashboard_users(), "can_manage": bool(user.get("is_admin"))}

    @app.post("/api/permissions/users")
    async def api_create_user(payload: CreateUserPayload, admin: dict[str, Any] = Depends(require_admin)):
        try:
            actor, actor_type = _actor_label(admin)
            created = await config_store.create_dashboard_user(
                payload.username, payload.password, role=payload.role, actor=actor, actor_type=actor_type
            )
            return {"ok": True, "item": created, "sync": config_store.sync_status()}
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.put("/api/permissions/users/{username}/password")
    async def api_change_password(
        username: str,
        payload: ChangePasswordPayload,
        admin: dict[str, Any] = Depends(require_admin),
    ):
        try:
            actor, actor_type = _actor_label(admin)
            ok = await config_store.update_dashboard_user_password(
                username, payload.password, actor=actor, actor_type=actor_type
            )
            if not ok:
                raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
            return {"ok": True}
        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/permissions/users/{username}")
    async def api_delete_user(username: str, admin: dict[str, Any] = Depends(require_admin)):
        uname = username.strip().lower()
        if admin.get("auth_type") == "password" and admin.get("username") == uname:
            raise HTTPException(status_code=400, detail="Không thể tự xóa tài khoản đang đăng nhập.")
        users = await config_store.list_dashboard_users()
        admins = [u for u in users if u.get("role") == "admin"]
        if len(admins) <= 1 and any(u["username"] == uname and u.get("role") == "admin" for u in users):
            # Chỉ chặn nếu cũng không có Discord Admin nào
            d_perms = await config_store.list_discord_permissions()
            if not any(p.get("is_admin") for p in d_perms):
                raise HTTPException(status_code=400, detail="Không thể xóa tài khoản Admin cuối cùng.")
        actor, actor_type = _actor_label(admin)
        deleted = await config_store.delete_dashboard_user(uname, actor=actor, actor_type=actor_type)
        if not deleted:
            raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
        return {"ok": True}

    @app.get("/api/audit-logs")
    async def api_audit_logs(
        limit: int = Query(default=100, ge=1, le=300),
        section: Optional[str] = Query(default=None),
        user: dict[str, Any] = Depends(require_user),
    ):
        return {"items": await config_store.list_audit_logs(limit=limit, section=section)}

    return app
