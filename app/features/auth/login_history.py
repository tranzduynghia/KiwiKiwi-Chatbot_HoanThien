"""Kho lưu trữ lịch sử đăng nhập tài khoản Admin (`admin_login_history`) có TTL 90 ngày và phân trang cursor."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.core.timezone import format_exact_timestamp, parse_user_agent_summary, parse_utc_datetime

log = logging.getLogger("gemini-bot.auth.history")

LOGIN_HISTORY_TTL_SECONDS = 90 * 24 * 3600  # 90 ngày tự động dọn bản ghi cũ trên MongoDB
MAX_MEMORY_HISTORY_ITEMS = 500
_SAFE_USERNAME_RE = re.compile(r"[^\w.@: -]", re.UNICODE)


def _sanitize_username(raw: Optional[str]) -> str:
    """Chuẩn hóa tên tài khoản (tối đa 64 ký tự), đảm bảo không bao giờ lưu mật khẩu hay chuỗi nhạy cảm."""
    cleaned = " ".join(str(raw or "").split()).strip()
    cleaned = _SAFE_USERNAME_RE.sub("", cleaned)
    return (cleaned or "unknown")[:64]


class AdminLoginHistoryRepository:
    """Ghi nhận mọi lượt đăng nhập Dashboard (thành công & thất bại) vào collection `admin_login_history`."""

    def __init__(self) -> None:
        self.collection: Any = None
        self._memory_logs: list[dict[str, Any]] = []

    @property
    def connected(self) -> bool:
        """Cho biết collection `admin_login_history` đã gắn MongoDB hay chưa."""
        return self.collection is not None

    async def attach_db(self, db: Any) -> None:
        """Gắn collection `admin_login_history` và tạo TTL index 90 ngày trên trường `created_at`."""
        self.collection = db["admin_login_history"]
        try:
            await self.collection.create_index("created_at", expireAfterSeconds=LOGIN_HISTORY_TTL_SECONDS)
            await self.collection.create_index([("status", 1), ("created_at", -1)])
        except Exception as exc:
            log.warning("Không tạo được index TTL 90 ngày cho admin_login_history: %s", exc)

    async def record_attempt(
        self,
        *,
        username: str,
        status: str,
        failure_reason: Optional[str] = None,
        auth_method: str = "password",
        ip_address: str = "unknown",
        country_code: str = "",
        client_timezone: str = "",
        user_agent: str = "",
        created_at: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Ghi nhận 1 lượt đăng nhập (tuyệt đối KHÔNG lưu mật khẩu, hash hay session token)."""
        now_utc = created_at or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)

        safe_status = "success" if str(status).strip().lower() == "success" else "failed"
        safe_method = "discord_oauth" if str(auth_method).strip().lower() in ("discord", "discord_oauth") else "password"
        safe_reason = str(failure_reason).strip()[:64] if failure_reason and safe_status == "failed" else None

        doc: dict[str, Any] = {
            "username": _sanitize_username(username),
            "status": safe_status,
            "failure_reason": safe_reason,
            "auth_method": safe_method,
            "ip_address": str(ip_address or "unknown").strip()[:64],
            "country_code": str(country_code or "").strip().upper()[:4],
            "client_timezone": str(client_timezone or "").strip()[:64],
            "device_summary": parse_user_agent_summary(user_agent, max_len=80),
            "created_at": now_utc,
        }

        self._memory_logs.insert(0, dict(doc))
        if len(self._memory_logs) > MAX_MEMORY_HISTORY_ITEMS:
            del self._memory_logs[MAX_MEMORY_HISTORY_ITEMS:]

        if self.connected:
            try:
                await self.collection.insert_one(dict(doc))
            except Exception as exc:
                log.warning("Không ghi được bản ghi admin_login_history lên MongoDB: %s", exc)
        return doc

    async def list_history(
        self,
        *,
        limit: int = 15,
        cursor: Optional[str] = None,
        status_filter: str = "all",
        tz_info: ZoneInfo,
    ) -> dict[str, Any]:
        """Truy vấn lịch sử đăng nhập Admin có phân trang cursor (15 mục/trang) và lọc trạng thái."""
        safe_limit = max(1, min(50, int(limit or 15)))
        norm_status = str(status_filter or "all").strip().lower()
        if norm_status not in ("all", "success", "failed"):
            norm_status = "all"

        cursor_offset = 0
        if cursor:
            try:
                cursor_offset = max(0, int(cursor))
            except (TypeError, ValueError):
                cursor_offset = 0

        if self.connected:
            flt: dict[str, Any] = {}
            if norm_status != "all":
                flt["status"] = norm_status
            try:
                total = await self.collection.count_documents(flt)
                raw_docs = (
                    await self.collection.find(flt)
                    .sort("created_at", -1)
                    .skip(cursor_offset)
                    .limit(safe_limit + 1)
                    .to_list(length=safe_limit + 1)
                )
            except Exception as exc:
                log.warning("Lỗi truy vấn admin_login_history: %s", exc)
                raw_docs = []
                total = 0
        else:
            filtered = (
                self._memory_logs
                if norm_status == "all"
                else [d for d in self._memory_logs if d.get("status") == norm_status]
            )
            total = len(filtered)
            raw_docs = filtered[cursor_offset : cursor_offset + safe_limit + 1]

        has_more = len(raw_docs) > safe_limit
        page_docs = raw_docs[:safe_limit]
        next_cursor = str(cursor_offset + safe_limit) if has_more else None
        prev_cursor = str(max(0, cursor_offset - safe_limit)) if cursor_offset > 0 else None

        items = []
        for doc in page_docs:
            dt = parse_utc_datetime(doc.get("created_at"))
            items.append(
                {
                    "username": str(doc.get("username") or "unknown"),
                    "status": str(doc.get("status") or "failed"),
                    "failure_reason": doc.get("failure_reason"),
                    "auth_method": str(doc.get("auth_method") or "password"),
                    "ip_address": str(doc.get("ip_address") or "unknown"),
                    "country_code": str(doc.get("country_code") or ""),
                    "client_timezone": str(doc.get("client_timezone") or ""),
                    "device_summary": str(doc.get("device_summary") or "Không xác định"),
                    "created_at": dt.isoformat() if dt else None,
                    "created_at_formatted": format_exact_timestamp(dt, tz_info),
                }
            )

        return {
            "items": items,
            "total": total,
            "limit": safe_limit,
            "status_filter": norm_status,
            "cursor": str(cursor_offset),
            "next_cursor": next_cursor,
            "prev_cursor": prev_cursor,
            "has_more": has_more,
        }
