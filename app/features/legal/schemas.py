"""Lược đồ Pydantic xác thực dữ liệu đầu vào cho CMS Điều khoản & Cổng chấp thuận ToS."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class UpdateLegalDocumentPayload(BaseModel):
    """Payload cập nhật nội dung một tài liệu pháp lý và/hoặc nâng phiên bản `active_tos_version`."""

    doc_key: Literal["legal", "terms", "policies", "guidelines"]
    title: str = Field(..., min_length=2, max_length=160)
    subtitle: str = Field(default="", max_length=240)
    body: str = Field(..., min_length=10, max_length=24000)
    active_tos_version: Optional[str] = Field(default=None, max_length=24)
    changes: Optional[dict[str, Any]] = None


class AcceptTosPayload(BaseModel):
    """Payload ghi nhận chấp thuận Điều khoản từ giao diện Web Dashboard / Login."""

    source: Literal["web", "discord"] = "web"
