"""Các schema Pydantic xác thực đầu vào cho API cấu hình /ask và System Instruction."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AskConfigPayload(BaseModel):
    """Payload cập nhật đồng thời cấu hình `/ask` và cấu hình phiên chat."""

    ask: dict[str, Any]
    session: dict[str, Any]
    changes: Optional[dict[str, dict[str, Any]]] = None


class PromptConfigPayload(BaseModel):
    """Payload cập nhật System Instruction và OCR Instruction."""

    system_prompt: str = Field(..., min_length=1, max_length=20000)
    ocr_instruction: str = Field(..., min_length=1, max_length=10000)
    append_current_date: bool = True
    changes: Optional[dict[str, dict[str, Any]]] = None


class PresenceConfigPayload(BaseModel):
    """Payload cập nhật trạng thái trực tuyến, Custom Status (bong bóng chữ), hoạt động chính và chế độ luân phiên."""

    status: str = Field(default="online", max_length=20)
    custom_status: str = Field(default="", max_length=128)
    activity_type: str = Field(default="listening", max_length=20)
    activity_text: str = Field(default="", max_length=128)
    auto_rotate: bool = False
    changes: Optional[dict[str, dict[str, Any]]] = None


class TunnelConfigPayload(BaseModel):
    """Payload bật/tắt hoặc cập nhật nóng cấu hình Cloudflare Tunnel (Named Token hoặc Quick Tunnel)."""

    cf_tunnel_enabled: bool = False
    cf_tunnel_mode: str = Field(default="named", max_length=20)
    cf_tunnel_token: Optional[str] = Field(default=None, max_length=4096)
    tunnel_only_inbound: bool = False
    proxy_headers: bool = False
    warp_outbound_enabled: Optional[bool] = None
    clear_token: bool = False
    changes: Optional[dict[str, dict[str, Any]]] = None


class WarpConfigPayload(BaseModel):
    """Payload bật/tắt nhanh định tuyến Outbound qua Cloudflare WARP User-Space Proxy (127.0.0.1:40000)."""

    warp_outbound_enabled: bool
    changes: Optional[dict[str, dict[str, Any]]] = None
