"""Kiểm thử tính năng quản lý Model Gemini: metadata, chuỗi Auto, ghi đè và cooldown lỗi."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.features.models.cooldown import ModelCooldownTracker
from app.features.models.metadata import default_badge_for, is_stable, tier_of, version_of


def test_model_metadata_helpers():
    """Kiểm tra trích xuất phiên bản, phân khúc, độ ổn định và nhãn mặc định của model."""
    assert version_of("gemini-2.5-flash-lite") == 2.5
    assert version_of("gemini-3.5-pro-preview") == 3.5
    assert tier_of("gemini-2.5-flash-lite") == "lite"
    assert tier_of("gemini-2.5-flash") == "flash"
    assert tier_of("gemini-3.5-pro") == "pro"
    assert is_stable("gemini-2.5-flash") is True
    assert is_stable("gemini-3-flash-preview") is False
    assert default_badge_for("gemini-3.5-pro") == "💳"
    assert default_badge_for("gemini-3-flash-preview") == "🧪"
    assert default_badge_for("gemini-2.5-flash") == "🆓"


@pytest.mark.asyncio
async def test_model_registry_refresh_auto_chain_and_choices(app_container):
    """Kiểm tra lọc model chat từ API, tính chuỗi Auto và áp dụng ẩn/hiện + overrides."""
    await app_container.model_registry.refresh()
    discovered_ids = [item.id for item in app_container.model_registry.text]
    assert "gemini-embedding-001" not in discovered_ids
    assert "gemini-3.5-flash" in discovered_ids

    # Chuỗi Auto mặc định: lite mới nhất -> flash mới nhất -> auto_last_model
    chain = app_container.model_registry.auto_chain()
    assert chain == ["gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-4-hydrogen"]

    # Cấu hình ghi đè tên hiển thị, tag và ẩn model
    await app_container.config_service.update_config(
        {
            "models": {
                "overrides": {
                    "gemini-3.5-flash": {"display_name": "Flash 3.5 Tùy Chỉnh", "tag": "⚡"},
                    "gemini-2.5-pro": {"hidden": False},
                }
            }
        },
        updated_by="admin",
    )
    choices_map = {item.id: item for item in app_container.model_registry.choices()}
    assert choices_map["gemini-3.5-flash"].display == "Flash 3.5 Tùy Chỉnh"
    assert choices_map["gemini-3.5-flash"].badge == "⚡"
    # Dù hide_pro_models=True và gemini-2.5-pro có trong hidden_models, override hidden=False vẫn ưu tiên hiện
    assert "gemini-2.5-pro" in choices_map


def test_model_cooldown_tracker_rules():
    """Kiểm tra tính thời gian cooldown cho TimeoutError, 404, 429 và xóa cooldown."""
    tracker = ModelCooldownTracker()
    err_429_retry = SimpleNamespace(code=429, message="Rate limit exceeded, retry in 12.5s")
    err_429_zero = SimpleNamespace(code=429, message="Quota limit: 0 for this tier")
    err_404 = SimpleNamespace(code=404, message="Model not found")

    assert tracker.cooldown_seconds(asyncio.TimeoutError()) == 60.0
    assert tracker.cooldown_seconds(err_429_retry) == 13.5
    assert tracker.cooldown_seconds(err_429_zero) == 3600.0
    assert tracker.cooldown_seconds(err_404) == 6 * 3600.0

    tracker.mark_failure("gemini-2.5-flash", err_429_retry)
    assert tracker.cooldown_left("gemini-2.5-flash") > 0
    assert tracker.clear_all() == 1
    assert tracker.cooldown_left("gemini-2.5-flash") == 0.0
