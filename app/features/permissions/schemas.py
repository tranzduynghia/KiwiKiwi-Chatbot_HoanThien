"""Schema Pydantic cho API quản lý phân quyền theo Discord User ID."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DiscordPermissionPayload(BaseModel):
    """Payload thêm mới hoặc cập nhật phân quyền cho một Discord User ID."""

    user_id: str = Field(..., min_length=5, max_length=30)
    label: str = Field(default="", max_length=80)
    is_admin: bool = False
    can_dashboard: bool = False
    can_status: bool = False
    changes: Optional[dict[str, dict[str, Any]]] = None
