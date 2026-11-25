"""Dịch vụ tra cứu thông tin người dùng (username, avatar) và vai trò server từ Discord client đang chạy."""
from __future__ import annotations

import time
from typing import Any, Optional


class DiscordUserLookupService:
    """Bổ sung avatar, username và danh sách vai trò server cho các bản ghi phiên chat với bộ đệm TTL."""

    def __init__(self, cache_ttl: float = 300.0):
        self._cache_ttl = cache_ttl
        self._user_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def list_server_roles(bot: Any) -> list[dict[str, str]]:
        """Thu thập danh sách vai trò (trừ `@everyone`) trên toàn bộ server mà bot đang tham gia."""
        if bot is None:
            return []
        roles_by_id: dict[str, dict[str, str]] = {}
        for guild in getattr(bot, "guilds", []) or []:
            guild_name = getattr(guild, "name", "")
            for role in getattr(guild, "roles", []) or []:
                role_name = getattr(role, "name", "")
                if not role_name or role_name == "@everyone":
                    continue
                rid = str(getattr(role, "id", ""))
                if not rid or rid in roles_by_id:
                    continue
                color_obj = getattr(role, "color", None)
                color_hex = str(color_obj) if color_obj and getattr(color_obj, "value", 0) else "#64748b"
                roles_by_id[rid] = {
                    "id": rid,
                    "name": role_name,
                    "color": color_hex,
                    "guild_name": guild_name,
                }
        return sorted(roles_by_id.values(), key=lambda item: (item["guild_name"].lower(), item["name"].lower()))

    @staticmethod
    def _extract_member_roles(member: Any) -> list[dict[str, str]]:
        """Trích xuất danh sách vai trò của một thành viên trong server (loại bỏ `@everyone`)."""
        roles: list[dict[str, str]] = []
        for role in getattr(member, "roles", []) or []:
            role_name = getattr(role, "name", "")
            if not role_name or role_name == "@everyone":
                continue
            color_obj = getattr(role, "color", None)
            color_hex = str(color_obj) if color_obj and getattr(color_obj, "value", 0) else "#64748b"
            roles.append({
                "id": str(getattr(role, "id", "")),
                "name": role_name,
                "color": color_hex,
            })
        return roles

    async def resolve_user_profile(self, bot: Any, user_id: str) -> dict[str, Any]:
        """Tra cứu `username`, `display_name`, `avatar_url` và `server_roles` cho một Discord User ID."""
        uid = str(user_id or "").strip()
        if not uid or bot is None:
            return {}

        now = time.monotonic()
        cached = self._user_cache.get(uid)
        if cached and (now - cached[0]) < self._cache_ttl:
            return cached[1]

        profile: dict[str, Any] = {}
        try:
            uid_int = int(uid)
        except ValueError:
            return {}

        # 1) Ưu tiên tìm Member trong các server để lấy đầy đủ cả avatar, username và server roles
        collected_roles: dict[str, dict[str, str]] = {}
        matched_user: Optional[Any] = None
        for guild in getattr(bot, "guilds", []) or []:
            get_member = getattr(guild, "get_member", None)
            member = get_member(uid_int) if callable(get_member) else None
            if member is not None:
                matched_user = matched_user or member
                for role_item in self._extract_member_roles(member):
                    collected_roles[role_item["id"]] = role_item

        # 2) Nếu chưa thấy trong guild cache -> thử bộ nhớ đệm user hoặc gọi fetch_user của Discord
        if matched_user is None:
            get_user = getattr(bot, "get_user", None)
            matched_user = get_user(uid_int) if callable(get_user) else None
        if matched_user is None:
            fetch_user = getattr(bot, "fetch_user", None)
            if callable(fetch_user):
                try:
                    matched_user = await fetch_user(uid_int)
                except Exception:
                    matched_user = None

        if matched_user is not None:
            avatar_obj = getattr(matched_user, "display_avatar", None)
            avatar_url = str(getattr(avatar_obj, "url", "") or "")
            profile = {
                "username": str(getattr(matched_user, "name", "") or ""),
                "display_name": str(
                    getattr(matched_user, "display_name", "")
                    or getattr(matched_user, "global_name", "")
                    or getattr(matched_user, "name", "")
                    or ""
                ),
                "avatar_url": avatar_url,
                "server_roles": list(collected_roles.values()),
            }

        self._user_cache[uid] = (now, profile)
        return profile

    def clear_cache(self) -> int:
        """Xóa toàn bộ bộ đệm hồ sơ Discord trong RAM và trả về số mục đã giải phóng."""
        count = len(self._user_cache)
        self._user_cache.clear()
        return count
