"""Router FastAPI phục vụ truy vấn nhật ký chỉnh sửa Audit Log kèm bổ sung avatar Discord."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query

from app.features.audit.repository import DEFAULT_AUDIT_PAGE_LIMIT, MAX_AUDIT_PAGE_LIMIT
from app.features.audit.service import AuditService
from app.features.sessions.discord_lookup import DiscordUserLookupService

DISCORD_ACTOR_RE = re.compile(r"\(discord:(\d+)\)")


def _parse_int_query(raw: Optional[str], *, default: int, min_val: int, max_val: Optional[int] = None) -> int:
    """Chuyển đổi tham số số nguyên trên query string một cách an toàn và kẹp trong khoảng hợp lệ thay vì ném lỗi 422."""
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError:
        return default
    if parsed < min_val:
        parsed = min_val
    if max_val is not None and parsed > max_val:
        parsed = max_val
    return parsed


def create_audit_router(
    audit_service: AuditService,
    auth_deps: Any,
    get_bot: Optional[Callable[[], Any]] = None,
    user_lookup: Optional[DiscordUserLookupService] = None,
) -> APIRouter:
    """Tạo router cho nhóm API `/api/audit-logs` và `/api/audit`."""
    router = APIRouter(tags=["audit"])
    lookup = user_lookup or DiscordUserLookupService()

    @router.get("/api/audit-logs")
    @router.get("/api/audit")
    async def api_audit_logs(
        page: Optional[str] = Query(default="1"),
        limit: Optional[str] = Query(default=str(DEFAULT_AUDIT_PAGE_LIMIT)),
        section: str = Query(default=""),
        category: str = Query(default=""),
        _user: dict[str, Any] = Depends(auth_deps.require_section_unlocked("audit")),
    ):
        safe_page = _parse_int_query(page, default=1, min_val=1, max_val=100_000)
        safe_limit = _parse_int_query(
            limit,
            default=DEFAULT_AUDIT_PAGE_LIMIT,
            min_val=1,
            max_val=MAX_AUDIT_PAGE_LIMIT,
        )
        effective_section = (section or category or "").strip()

        result = await audit_service.list_logs_paginated(
            page=safe_page,
            limit=safe_limit,
            section=effective_section,
        )
        items = result["items"]
        bot = get_bot() if callable(get_bot) else None

        # Chỉ bổ sung avatar và Discord ID cho đúng các bản ghi thuộc trang hiện tại
        for item in items:
            actor_str = str(item.get("actor") or "")
            discord_id = str(item.get("actor_discord_id") or "")
            if not discord_id:
                match = DISCORD_ACTOR_RE.search(actor_str)
                if match:
                    discord_id = match.group(1)
                    item["actor_discord_id"] = discord_id
            if not item.get("actor_display_name"):
                item["actor_display_name"] = DISCORD_ACTOR_RE.sub("", actor_str).strip() or actor_str
            if discord_id and not item.get("actor_avatar_url") and bot is not None:
                profile = await lookup.resolve_user_profile(bot, discord_id)
                if profile.get("avatar_url"):
                    item["actor_avatar_url"] = profile["avatar_url"]

        return {
            "items": items,
            "page": result["page"],
            "limit": result["limit"],
            "total_items": result["total_items"],
            "total_pages": result["total_pages"],
        }

    return router
