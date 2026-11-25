"""Router FastAPI phục vụ truy vấn nhật ký chỉnh sửa Audit Log kèm bổ sung avatar Discord."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, Query

from app.features.audit.service import AuditService
from app.features.sessions.discord_lookup import DiscordUserLookupService

DISCORD_ACTOR_RE = re.compile(r"\(discord:(\d+)\)")


def create_audit_router(
    audit_service: AuditService,
    auth_deps: Any,
    get_bot: Optional[Callable[[], Any]] = None,
    user_lookup: Optional[DiscordUserLookupService] = None,
) -> APIRouter:
    """Tạo router cho nhóm API `/api/audit-logs`."""
    router = APIRouter(tags=["audit"])
    lookup = user_lookup or DiscordUserLookupService()

    @router.get("/api/audit-logs")
    async def api_audit_logs(
        limit: int = Query(default=100, ge=1, le=300),
        section: Optional[str] = Query(default=None),
        _user: dict[str, Any] = Depends(auth_deps.require_user),
    ):
        items = await audit_service.list_logs(limit=limit, section=section)
        bot = get_bot() if callable(get_bot) else None

        # Bổ sung avatar và Discord ID cho các bản ghi Audit Log cũ từ Discord client đang chạy
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

        return {"items": items}

    return router
