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

SENSITIVE_KEY_TOKENS = ("password", "secret", "token", "api_key", "apikey", "uri")


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
    cache_ttl_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "AppSettings":
        """Khởi tạo đối tượng cấu hình từ toàn bộ biến môi trường hiện tại."""
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
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": "system-default",
    }
