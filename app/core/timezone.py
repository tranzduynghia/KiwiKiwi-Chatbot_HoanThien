"""Tiện ích phân giải múi giờ IANA từ header Cloudflare (`CF-Timezone`), định dạng thời gian chính xác từng giây và phân tích User-Agent."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_IANA_TIMEZONE = "Asia/Ho_Chi_Minh"
_VALID_TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def validate_iana_timezone(raw: Optional[str]) -> Optional[tuple[str, ZoneInfo]]:
    """Xác thực chuỗi múi giờ IANA qua `zoneinfo.ZoneInfo`; trả về `(tz_name, ZoneInfo)` nếu hợp lệ."""
    candidate = str(raw or "").strip()
    if not candidate or not _VALID_TZ_RE.fullmatch(candidate):
        return None
    try:
        return candidate, ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def resolve_request_timezone(
    headers: Any,
    *,
    fallback_tz: Optional[str] = None,
) -> tuple[str, ZoneInfo]:
    """Ưu tiên `CF-Timezone` từ Cloudflare Edge, kế đến múi giờ trình duyệt `fallback_tz`, cuối cùng mặc định `Asia/Ho_Chi_Minh`."""
    cf_tz = ""
    if headers is not None:
        cf_tz = str(headers.get("cf-timezone") or headers.get("CF-Timezone") or "").strip()
    for candidate in (cf_tz, fallback_tz, DEFAULT_IANA_TIMEZONE):
        resolved = validate_iana_timezone(candidate)
        if resolved is not None:
            return resolved
    return "UTC", ZoneInfo("UTC")


def format_utc_offset(tz_info: ZoneInfo, ref_dt: Optional[datetime] = None) -> str:
    """Định dạng độ lệch UTC gọn gàng, ví dụ `UTC+7`, `UTC-8`, `UTC+5:30`."""
    now_dt = ref_dt or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    offset = now_dt.astimezone(tz_info).utcoffset()
    if offset is None:
        return "UTC+0"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    abs_min = abs(total_minutes)
    hours, minutes = divmod(abs_min, 60)
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def format_timezone_badge(tz_name: str, tz_info: ZoneInfo, ref_dt: Optional[datetime] = None) -> str:
    """Tạo chuỗi hiển thị múi giờ chuẩn cho Dashboard, ví dụ `Asia/Ho_Chi_Minh (UTC+7)`."""
    return f"{tz_name} ({format_utc_offset(tz_info, ref_dt)})"


def parse_utc_datetime(value: Any) -> Optional[datetime]:
    """Chuyển đổi `datetime` hoặc chuỗi ISO-8601 sang `datetime` có múi giờ UTC."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def format_exact_timestamp(value: Any, tz_info: ZoneInfo) -> str:
    """Định dạng mốc thời gian chính xác tới từng giây `HH:mm:ss DD/MM/YYYY` theo múi giờ IP truy cập."""
    dt = parse_utc_datetime(value)
    if dt is None:
        return "—"
    return dt.astimezone(tz_info).strftime("%H:%M:%S %d/%m/%Y")


def extract_cf_country(headers: Any) -> str:
    """Đọc mã quốc gia 2 ký tự từ header `CF-IPCountry` của Cloudflare (nếu hợp lệ)."""
    if headers is None:
        return ""
    raw = str(headers.get("cf-ipcountry") or headers.get("CF-IPCountry") or "").strip().upper()
    if raw in ("XX", "T1"):
        return raw
    return raw if _COUNTRY_RE.fullmatch(raw) else ""


def parse_user_agent_summary(user_agent: Optional[str], max_len: int = 80) -> str:
    """Phân tích an toàn tên Trình duyệt & Hệ điều hành ngắn gọn từ User-Agent (tối đa 80 ký tự)."""
    ua = " ".join(str(user_agent or "").split()).strip()
    if not ua:
        return "Không xác định"

    lower = ua.lower()
    # Nhận diện trình duyệt phổ biến theo thứ tự ưu tiên tránh nhầm Chrome/Safari
    if "edg/" in lower or "edge/" in lower:
        browser = "Edge"
    elif "opr/" in lower or "opera" in lower:
        browser = "Opera"
    elif "brave" in lower:
        browser = "Brave"
    elif "firefox/" in lower or "fxios/" in lower:
        browser = "Firefox"
    elif "chrome/" in lower or "crios/" in lower:
        browser = "Chrome"
    elif "safari/" in lower:
        browser = "Safari"
    elif "curl/" in lower:
        browser = "curl"
    elif "python" in lower or "httpx" in lower:
        browser = "Python HTTP"
    else:
        browser = "Trình duyệt khác"

    # Nhận diện hệ điều hành
    if "windows nt 10" in lower or "windows nt 11" in lower:
        os_name = "Windows"
    elif "windows" in lower:
        os_name = "Windows"
    elif "iphone" in lower or "ipad" in lower or "cpu os" in lower:
        os_name = "iOS"
    elif "android" in lower:
        os_name = "Android"
    elif "mac os x" in lower or "macintosh" in lower:
        os_name = "macOS"
    elif "linux" in lower:
        os_name = "Linux"
    else:
        os_name = "OS khác"

    summary = f"{browser} · {os_name}"
    return summary[:max_len]
