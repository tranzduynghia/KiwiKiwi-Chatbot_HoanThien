"""
Quản lý biến môi trường, hằng số hệ thống và cấu hình mặc định khởi tạo cho bot.
Mọi hàm đọc biến môi trường được tách khỏi thời điểm import để dễ kiểm thử độc lập.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

BOT_VERSION = "2026.11.24-dashboard-atlas"
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

DEFAULT_SYSTEM_PROMPT = (
    "Bạn là trợ lý AI trong Discord. Trả lời bằng đúng ngôn ngữ người dùng đang dùng "
    "(mặc định tiếng Việt), rõ ràng, súc tích. Dùng Markdown của Discord "
    "(in đậm, danh sách, code block); KHÔNG dùng bảng Markdown vì Discord không hiển thị được."
)

DEFAULT_OCR_INSTRUCTION = (
    "Bạn là công cụ OCR. Hãy chép lại CHÍNH XÁC toàn bộ chữ có trong tệp đính kèm, giữ nguyên "
    "thứ tự và bố cục (dùng danh sách/code block nếu cần, bảng thì trình bày dạng dòng chữ). "
    "Không tự thêm bình luận. Nếu người dùng có yêu cầu thêm, hãy làm sau khi chép xong."
)

DEFAULT_HIDDEN_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-nano-banana-2.1",
    "gemini-omni-flash-preview",
    "gemini-4-argon",
    "gemini-4-carbon",
]

DEFAULT_FALLBACK_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-4-hydrogen",
]

SENSITIVE_KEY_TOKENS = (
    "password",
    "pepper",
    "secret",
    "token",
    "api_key",
    "apikey",
    "uri",
    "salt",
    "hash",
    "signing_key",
    "private_key",
    "license_key",
    "credential",
)


def parse_csv_env(name: str, default: str = "") -> list[str]:
    """Đọc danh sách chuỗi phân tách bằng dấu phẩy hoặc xuống dòng từ biến môi trường."""
    raw = os.getenv(name)
    if raw is None:
        raw = default
    return [item.strip() for item in re.split(r"[,\n]+", raw) if item.strip()]


def parse_bool_env(name: str, default: bool) -> bool:
    """Đọc giá trị boolean từ biến môi trường với giá trị mặc định an toàn."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "")


def parse_float_env(name: str, default: float) -> float:
    """Đọc số thực từ biến môi trường, trả về mặc định nếu không hợp lệ."""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_int_env(name: str, default: int) -> int:
    """Đọc số nguyên từ biến môi trường, trả về mặc định nếu không hợp lệ."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_optional_int_env(name: str) -> Optional[int]:
    """Đọc số nguyên dương tùy chọn từ biến môi trường."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
        return value if value > 0 else None
    except ValueError:
        return None


@dataclass(frozen=True)
class AppSettings:
    """Cấu hình cấp tiến trình đọc từ biến môi trường khi khởi động."""

    discord_token: str
    gemini_api_key: str
    port: int = 10000
    guild_id: Optional[str] = None
    mongodb_uri: str = ""
    mongodb_db: str = "gemini_bot"
    gemini_api_version: str = ""
    session_secret: str = ""
    dashboard_public_url: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    admin_discord_id: str = ""
    outbound_proxy_url: str = ""
    warp_proxy_port: int = 40000
    warp_outbound_enabled: bool = True
    cache_ttl_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "AppSettings":
        """Khởi tạo đối tượng cấu hình từ toàn bộ biến môi trường hiện tại."""
        raw_warp_port = parse_int_env("WARP_PROXY_PORT", 40000)
        warp_port = raw_warp_port if 1 <= raw_warp_port <= 65535 else 40000
        warp_default = parse_bool_env("WARP_OUTBOUND_ENABLED", parse_bool_env("WARP_ENABLED", True))
        return cls(
            discord_token=(os.getenv("DISCORD_TOKEN") or "").strip(),
            gemini_api_key=(os.getenv("GEMINI_API_KEY") or "").strip(),
            port=parse_int_env("PORT", 10000),
            guild_id=(os.getenv("GUILD_ID") or "").strip() or None,
            mongodb_uri=(os.getenv("MONGODB_URI") or "").strip(),
            mongodb_db=(os.getenv("MONGODB_DB") or "gemini_bot").strip() or "gemini_bot",
            gemini_api_version=(os.getenv("GEMINI_API_VERSION") or "").strip(),
            session_secret=(os.getenv("SESSION_SECRET") or "").strip(),
            dashboard_public_url=(os.getenv("DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/"),
            discord_client_id=(os.getenv("DISCORD_CLIENT_ID") or "").strip(),
            discord_client_secret=(os.getenv("DISCORD_CLIENT_SECRET") or "").strip(),
            discord_redirect_uri=(os.getenv("DISCORD_REDIRECT_URI") or "").strip(),
            admin_discord_id=(os.getenv("ADMIN_DISCORD_ID") or "").strip(),
            outbound_proxy_url=(os.getenv("OUTBOUND_PROXY_URL") or "").strip(),
            warp_proxy_port=warp_port,
            warp_outbound_enabled=warp_default,
        )


def build_env_tunnel_defaults() -> dict[str, Any]:
    """Đọc cấu hình Cloudflare Tunnel, WARP Outbound & Proxy Headers mặc định từ biến môi trường khi khởi động container."""
    env_token = (os.getenv("CF_TUNNEL_TOKEN") or os.getenv("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
    raw_mode = (os.getenv("CF_TUNNEL_MODE") or "").strip().lower()
    mode = raw_mode if raw_mode in ("named", "quick") else "named"

    tunnel_only = parse_bool_env("CF_TUNNEL_ONLY", False)
    proxy_headers = parse_bool_env("TRUST_PROXY_HEADERS", False)
    warp_outbound_enabled = parse_bool_env("WARP_OUTBOUND_ENABLED", parse_bool_env("WARP_ENABLED", True))

    # Tự động bật Tunnel nếu cung cấp token hoặc đặt rõ CF_TUNNEL_MODE=quick (trừ khi CF_TUNNEL_ENABLED=false)
    default_enabled = bool(env_token) if mode == "named" else (raw_mode == "quick")
    enabled = parse_bool_env("CF_TUNNEL_ENABLED", default_enabled)

    return {
        "cf_tunnel_enabled": enabled,
        "cf_tunnel_mode": mode,
        "cf_tunnel_token": env_token,
        "tunnel_only_inbound": tunnel_only,
        "proxy_headers": proxy_headers,
        "warp_outbound_enabled": warp_outbound_enabled,
    }


def has_env_tunnel_overrides() -> bool:
    """Kiểm tra có biến môi trường cấu hình Cloudflare Tunnel / Proxy Headers nào được đặt hay không."""
    return any(
        os.getenv(var) is not None and (os.getenv(var) or "").strip() != ""
        for var in (
            "CF_TUNNEL_TOKEN",
            "CLOUDFLARE_TUNNEL_TOKEN",
            "CF_TUNNEL_MODE",
            "CF_TUNNEL_ONLY",
            "TRUST_PROXY_HEADERS",
            "CF_TUNNEL_ENABLED",
        )
    )


def build_default_config() -> dict[str, Any]:
    """Tạo tài liệu cấu hình động mặc định cho collection `bot_config`."""
    return {
        "_id": "global",
        "system_prompt": os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        "ocr_instruction": os.getenv("OCR_INSTRUCTION", DEFAULT_OCR_INSTRUCTION),
        "append_current_date": True,
        "ask": {
            "user_cooldown": parse_float_env("USER_COOLDOWN", 5.0),
            "max_concurrent": max(1, parse_int_env("MAX_CONCURRENT", 2)),
            "max_file_mb": parse_float_env("MAX_FILE_MB", 10.0),
            "allow_file": True,
            "allow_ocr": True,
            "allow_web_search": True,
            "allow_thinking": True,
            "default_web_search": False,
            "default_thinking_level": "",
            "thinking_budgets": {"low": 1024, "medium": 8192, "high": 24576},
            "gemini_timeout_seconds": parse_float_env("GEMINI_TIMEOUT_SECONDS", 170.0),
            "max_retries": max(1, parse_int_env("MAX_RETRIES", 3)),
            "chunk_char_limit": 1900,
            "max_reply_chunks": 6,
            "show_thoughts_embed": True,
            "show_sources": True,
        },
        "session": {
            "session_idle_hours": parse_float_env("SESSION_IDLE_HOURS", 2.0),
            "max_history_messages": max(2, parse_int_env("MAX_HISTORY_MESSAGES", 20)),
            "max_history_chars": max(1000, parse_int_env("MAX_HISTORY_CHARS", 24000)),
            "max_stored_chars": max(500, parse_int_env("MAX_STORED_CHARS", 4000)),
            "session_max_turns": max(1, parse_int_env("SESSION_MAX_TURNS", 500)),
            "sweep_minutes": max(1.0, parse_float_env("SWEEP_MINUTES", 5.0)),
            "mongodb_quota_mb": max(1.0, parse_float_env("MONGODB_QUOTA_MB", 512.0)),
            "ram_limit_mb": parse_optional_int_env("RAM_LIMIT_MB"),
        },
        "models": {
            "default_text_model": os.getenv("DEFAULT_TEXT_MODEL", "").strip(),
            "auto_model_chain": parse_csv_env("AUTO_MODEL_CHAIN", ""),
            "auto_last_model": os.getenv("AUTO_LAST_MODEL", "gemini-4-hydrogen").strip(),
            "fallback_text": list(DEFAULT_FALLBACK_MODELS),
            "hidden_models": parse_csv_env("HIDDEN_MODELS", ",".join(DEFAULT_HIDDEN_MODELS)),
            "hide_pro_models": parse_bool_env("HIDE_PRO_MODELS", True),
            "extra_models": parse_csv_env("EXTRA_MODELS", "gemini-4-hydrogen"),
            "model_refresh_hours": max(0.25, parse_float_env("MODEL_REFRESH_HOURS", 6.0)),
            "overrides": {},
        },
        "presence": {
            "status": "online",
            "custom_status": "",
            "activity_type": "listening",
            "activity_text": "m!help",
            "auto_rotate": False,
        },
        "tunnel": build_env_tunnel_defaults(),
        "legal": _default_legal_config(),
        "security": {
            "locked_sections": ["perms", "sessions", "temp"],
            "step_up_cooldown_sec": 60,
            "presence_interval_sec": 20,
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system-default",
    }


def _default_legal_config() -> dict[str, Any]:
    """Nạp cấu trúc nhánh `legal` mặc định tránh vòng lặp import khi khởi tạo cấu hình."""
    from app.features.legal.defaults import build_default_legal_config

    return build_default_legal_config()
