"""Schema Pydantic cho API cấu hình Model Gemini."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ModelsConfigPayload(BaseModel):
    """Payload cập nhật danh sách model ẩn, extra, chuỗi Auto và ghi đè theo từng model."""

    default_text_model: str = ""
    auto_model_chain: list[str] = Field(default_factory=list)
    auto_last_model: str = "gemini-4-hydrogen"
    fallback_text: list[str] = Field(default_factory=list)
    hidden_models: list[str] = Field(default_factory=list)
    hide_pro_models: bool = True
    extra_models: list[str] = Field(default_factory=list)
    model_refresh_hours: float = 6.0
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
