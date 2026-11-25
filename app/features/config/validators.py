"""Kiểm tra biên giá trị và chuẩn hóa dữ liệu cấu hình /ask và phiên chat."""
from __future__ import annotations

from typing import Any

ALLOWED_THINKING_LEVELS = ("", "auto", "low", "medium", "high", "xhigh")


def sanitize_ask_and_session(
    ask_in: dict[str, Any],
    session_in: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Chuẩn hóa toàn bộ tham số `/ask` và `session`, ném ValueError nếu vượt quy tắc."""
    thinking_level = str(ask_in.get("default_thinking_level") or "").strip().lower()
    if thinking_level not in ALLOWED_THINKING_LEVELS:
        raise ValueError("default_thinking_level không hợp lệ.")

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
        "default_thinking_level": thinking_level,
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

    raw_ram = session_in.get("ram_limit_mb")
    ram_limit = None
    if raw_ram not in (None, "", 0, "0"):
        ram_limit = max(32, int(raw_ram))

    clean_session = {
        "session_idle_hours": max(0.1, float(session_in.get("session_idle_hours", 2.0))),
        "max_history_messages": max(2, min(200, int(session_in.get("max_history_messages", 20)))),
        "max_history_chars": max(1000, int(session_in.get("max_history_chars", 24000))),
        "max_stored_chars": max(500, int(session_in.get("max_stored_chars", 4000))),
        "session_max_turns": max(1, int(session_in.get("session_max_turns", 500))),
        "sweep_minutes": max(1.0, float(session_in.get("sweep_minutes", 5.0))),
        "mongodb_quota_mb": max(1.0, float(session_in.get("mongodb_quota_mb", 512.0))),
        "ram_limit_mb": ram_limit,
    }

    return clean_ask, clean_session


ALLOWED_PRESENCE_STATUSES = ("online", "idle", "dnd", "invisible")
ALLOWED_ACTIVITY_TYPES = ("none", "playing", "listening", "watching", "competing")


def sanitize_presence_config(
    status: str,
    activity_type: str,
    activity_text: str,
    custom_status: str = "",
) -> dict[str, str]:
    """Kiểm tra hợp lệ trạng thái trực tuyến, Custom Status (bong bóng chữ) và hoạt động chính của bot."""
    clean_status = str(status or "online").strip().lower()
    if clean_status not in ALLOWED_PRESENCE_STATUSES:
        raise ValueError("Trạng thái hiển thị không hợp lệ (chỉ nhận online, idle, dnd, invisible).")

    clean_type = str(activity_type or "none").strip().lower()
    clean_custom = str(custom_status or "").strip()[:128]
    clean_text = str(activity_text or "").strip()[:128]

    # Tương thích ngược nếu client cũ gửi activity_type="custom": chuyển nội dung sang custom_status
    if clean_type == "custom":
        if not clean_custom and clean_text:
            clean_custom = clean_text
        clean_type = "none"
        clean_text = ""

    if clean_type not in ALLOWED_ACTIVITY_TYPES:
        raise ValueError("Loại hoạt động chính không hợp lệ (none, playing, listening, watching, competing).")

    if clean_type == "none":
        clean_text = ""

    return {
        "status": clean_status,
        "custom_status": clean_custom,
        "activity_type": clean_type,
        "activity_text": clean_text,
    }
