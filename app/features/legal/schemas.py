"""Lược đồ Pydantic xác thực dữ liệu đầu vào cho CMS Điều khoản 5 trang & Cổng chấp thuận ToS."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class UpdateLegalDocumentPayload(BaseModel):
    """Payload cập nhật nội dung Rich-Text một tài liệu pháp lý và/hoặc nâng phiên bản `active_tos_version`."""

    doc_key: Literal["toslinks", "legal", "terms", "policies", "guidelines"]
    title: str = Field(..., min_length=2, max_length=160)
    subtitle: str = Field(default="", max_length=240)
    body: str = Field(..., min_length=10, max_length=48000)
    active_tos_version: Optional[str] = Field(default=None, max_length=24)
    changes: Optional[dict[str, Any]] = None


class AcceptTosPayload(BaseModel):
    """Payload ghi nhận chấp thuận Điều khoản từ giao diện Web Dashboard / Login."""

    source: Literal["web", "discord"] = "web"


class UpdateDiscordPromptPayload(BaseModel):
    """Payload cập nhật mẫu tin nhắn yêu cầu đồng ý ToS trên Discord và nhãn nút xác nhận."""

    discord_tos_welcome_template: str = Field(..., min_length=10, max_length=1500)
    discord_tos_update_template: str = Field(..., min_length=10, max_length=1500)
    discord_tos_button_label: str = Field(..., min_length=1, max_length=60)
    changes: Optional[dict[str, Any]] = None


class SaveTempDocumentPayload(BaseModel):
    """Payload lưu nội dung Rich-Text vào trang nháp `/temp` trên MongoDB."""

    title: str = Field(..., min_length=2, max_length=160)
    subtitle: str = Field(default="", max_length=240)
    body: str = Field(..., min_length=10, max_length=48000)
    changes: Optional[dict[str, Any]] = None


class PushTempDocumentPayload(BaseModel):
    """Payload đẩy nội dung từ trang nháp `/temp` sang 1 trong 5 trang pháp lý công khai."""

    target_doc_key: Literal["toslinks", "legal", "terms", "policies", "guidelines"]
    title: Optional[str] = Field(default=None, min_length=2, max_length=160)
    subtitle: Optional[str] = Field(default=None, max_length=240)
    body: Optional[str] = Field(default=None, min_length=10, max_length=48000)
    active_tos_version: Optional[str] = Field(default=None, max_length=24)
    changes: Optional[dict[str, Any]] = None

