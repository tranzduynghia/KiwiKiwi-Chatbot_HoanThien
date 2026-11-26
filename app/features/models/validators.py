"""Chuẩn hóa dữ liệu cấu hình Model Gemini trước khi lưu vào MongoDB."""
from __future__ import annotations

import re
from typing import Any

from app.core.settings import DEFAULT_FALLBACK_MODELS
from app.features.models.schemas import ModelsConfigPayload

# Chặn khóa MongoDB bắt đầu bằng `$`, chứa ký tự null hoặc vượt độ dài (cho phép dấu `.` trong tên model như `gemini-2.5-flash` nhưng chặn `..` và bắt đầu/kết thúc bằng `.`)
SAFE_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/*-]{0,119}$")


def _clean_model_list(items: list[str], *, max_items: int = 100) -> list[str]:
    result: list[str] = []
    for raw in (items or [])[:max_items]:
        cleaned = str(raw or "").strip()
        if cleaned and not cleaned.startswith("$") and "\x00" not in cleaned and len(cleaned) <= 120:
            result.append(cleaned)
    return result


def sanitize_models_config(payload: ModelsConfigPayload) -> dict[str, Any]:
    """Làm sạch danh sách model, chặn khóa MongoDB nguy hiểm (`$...`), giới hạn độ dài tên hiển thị/tag và chuẩn hóa overrides."""
    clean_overrides: dict[str, dict[str, Any]] = {}
    for model_id, override in list((payload.overrides or {}).items())[:200]:
        clean_id = str(model_id or "").strip()
        if (
            not clean_id
            or clean_id.startswith("$")
            or "\x00" in clean_id
            or not SAFE_MODEL_ID_RE.fullmatch(clean_id)
            or not isinstance(override, dict)
        ):
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
        "default_text_model": payload.default_text_model.strip()[:120],
        "auto_model_chain": _clean_model_list(payload.auto_model_chain),
        "auto_last_model": payload.auto_last_model.strip()[:120],
        "fallback_text": _clean_model_list(payload.fallback_text) or list(DEFAULT_FALLBACK_MODELS),
        "hidden_models": _clean_model_list(payload.hidden_models),
        "hide_pro_models": bool(payload.hide_pro_models),
        "extra_models": _clean_model_list(payload.extra_models),
        "model_refresh_hours": max(0.25, min(168.0, float(payload.model_refresh_hours))),
        "overrides": clean_overrides,
    }
