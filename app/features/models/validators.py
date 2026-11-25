"""Chuẩn hóa dữ liệu cấu hình Model Gemini trước khi lưu vào MongoDB."""
from __future__ import annotations

from typing import Any

from app.core.settings import DEFAULT_FALLBACK_MODELS
from app.features.models.schemas import ModelsConfigPayload


def sanitize_models_config(payload: ModelsConfigPayload) -> dict[str, Any]:
    """Làm sạch danh sách model, giới hạn độ dài tên hiển thị/tag và chuẩn hóa overrides."""
    clean_overrides: dict[str, dict[str, Any]] = {}
    for model_id, override in (payload.overrides or {}).items():
        clean_id = str(model_id).strip()
        if not clean_id or not isinstance(override, dict):
            continue
        entry: dict[str, Any] = {}
        if "display_name" in override and str(override["display_name"]).strip():
            entry["display_name"] = str(override["display_name"]).strip()[:80]
        if "tag" in override and str(override["tag"]).strip():
            entry["tag"] = str(override["tag"]).strip()[:16]
        if "is_free" in override and override["is_free"] is not None:
            entry["is_free"] = bool(override["is_free"])
        if "hidden" in override and override["hidden"] is not None:
            entry["hidden"] = bool(override["hidden"])
        if entry:
            clean_overrides[clean_id] = entry

    return {
        "default_text_model": payload.default_text_model.strip(),
        "auto_model_chain": [item.strip() for item in payload.auto_model_chain if item and item.strip()],
        "auto_last_model": payload.auto_last_model.strip(),
        "fallback_text": [item.strip() for item in payload.fallback_text if item and item.strip()]
        or list(DEFAULT_FALLBACK_MODELS),
        "hidden_models": [item.strip() for item in payload.hidden_models if item and item.strip()],
        "hide_pro_models": bool(payload.hide_pro_models),
        "extra_models": [item.strip() for item in payload.extra_models if item and item.strip()],
        "model_refresh_hours": max(0.25, float(payload.model_refresh_hours)),
        "overrides": clean_overrides,
    }
