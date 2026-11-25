"""Các schema Pydantic xác thực đầu vào cho API cấu hình /ask và System Instruction."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AskConfigPayload(BaseModel):
    """Payload cập nhật đồng thời cấu hình `/ask` và cấu hình phiên chat."""

    ask: dict[str, Any]
    session: dict[str, Any]


class PromptConfigPayload(BaseModel):
    """Payload cập nhật System Instruction và OCR Instruction."""

    system_prompt: str = Field(..., min_length=1, max_length=20000)
    ocr_instruction: str = Field(..., min_length=1, max_length=10000)
    append_current_date: bool = True
