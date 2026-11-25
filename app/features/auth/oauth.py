"""Dịch vụ tích hợp Discord OAuth2 (tạo URL ủy quyền, đổi authorization code, lấy hồ sơ @me)."""
from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import httpx
from fastapi import Request

log = logging.getLogger("gemini-bot.auth.oauth")

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


class DiscordOAuthError(RuntimeError):
    """Lỗi nghiệp vụ khi trao đổi token hoặc lấy thông tin người dùng từ Discord OAuth2."""


class DiscordOAuthService:
    """Đóng gói toàn bộ luồng gọi Discord OAuth2 tách biệt khỏi router FastAPI."""

    def resolve_config(self, request: Request) -> tuple[str, str, str]:
        """Đọc Client ID, Client Secret và tự suy ra Redirect URI phù hợp với request hiện tại."""
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

    def is_enabled(self, request: Request) -> bool:
        """Kiểm tra đã cấu hình đủ Client ID và Client Secret để bật nút Login with Discord chưa."""
        client_id, client_secret, _ = self.resolve_config(request)
        return bool(client_id and client_secret)

    def build_authorize_url(self, client_id: str, redirect_uri: str, state: str) -> str:
        """Tạo đường dẫn chuyển hướng người dùng sang trang cấp quyền Discord OAuth2."""
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
        })
        return f"{DISCORD_AUTHORIZE_URL}?{params}"

    async def fetch_discord_user(
        self,
        *,
        code: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
    ) -> dict:
        """Đổi mã `code` lấy access token rồi gọi endpoint `/users/@me` của Discord."""
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
                    raise DiscordOAuthError(
                        "Không đổi được mã xác thực với Discord (kiểm tra Redirect URI / Client Secret)."
                    )
                access_token = token_res.json().get("access_token")
                user_res = await client.get(
                    DISCORD_USER_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if user_res.status_code != 200:
                    raise DiscordOAuthError("Không lấy được thông tin tài khoản Discord.")
                return user_res.json()
        except DiscordOAuthError:
            raise
        except Exception as exc:
            log.warning("Lỗi gọi Discord OAuth2: %s", exc)
            raise DiscordOAuthError(f"Lỗi kết nối Discord OAuth2: {type(exc).__name__}") from exc
