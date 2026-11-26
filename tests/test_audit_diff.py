"""Kiểm thử tính toán diff và ẩn trường nhạy cảm của tính năng Audit Log."""
from __future__ import annotations

from app.features.audit.diff import (
    compute_diff,
    is_sensitive_field,
    mask_secret_preview,
    merge_client_changes,
    sanitize_audit_value,
)


def test_sensitive_field_detection():
    """Đảm bảo mọi đường dẫn chứa mật khẩu, secret, token, api_key, uri đều bị ẩn."""
    assert is_sensitive_field("password_hash") is True
    assert is_sensitive_field("discord.client_secret") is True
    assert is_sensitive_field("gemini.api_key") is True
    assert is_sensitive_field("ask.user_cooldown") is False


def test_compute_diff_filters_metadata_and_sensitive_keys():
    """Kiểm tra `compute_diff` bỏ qua `_id`, `updated_at`, `updated_by` và các trường nhạy cảm."""
    old_doc = {
        "_id": "global",
        "updated_at": "2026-01-01",
        "updated_by": "a",
        "ask": {"user_cooldown": 5.0, "max_retries": 3},
        "api_token": "old-secret",
    }
    new_doc = {
        "_id": "global",
        "updated_at": "2026-01-02",
        "updated_by": "b",
        "ask": {"user_cooldown": 2.0, "max_retries": 3},
        "api_token": "new-secret",
    }
    diff = compute_diff(old_doc, new_doc)
    assert diff == [{"field": "ask.user_cooldown", "before": 5.0, "after": 2.0}]


def test_sanitize_long_audit_value():
    """Kiểm tra chuỗi dài hơn 500 ký tự được cắt gọn kèm tổng độ dài."""
    long_text = "x" * 600
    sanitized = sanitize_audit_value(long_text)
    assert sanitized.endswith("… (600 ký tự)")
    assert len(sanitized) < 530


def test_mask_secret_preview_and_merge_client_changes():
    """Kiểm tra che bí mật tuyệt đối không lộ tiền tố (`••••••••••••`) và gộp `changes` từ Modal an toàn."""
    assert mask_secret_preview("") == "(trống)"
    assert mask_secret_preview("abc") == "••••••••••••"
    assert mask_secret_preview("eyJhIjoi1234567890") == "••••••••••••"
    assert mask_secret_preview("eyJh•••••") == "••••••••••••"

    server_changes = [{"field": "tunnel.cf_tunnel_enabled", "before": False, "after": True}]
    client_changes = {
        "tunnel.cf_tunnel_enabled": {"old": False, "new": True},
        "tunnel.cf_tunnel_token": {"old": "", "new": "eyJhSecretRawValueShouldBeMasked"},
    }
    merged = merge_client_changes(server_changes, client_changes)
    assert len(merged) == 2
    token_entry = next(item for item in merged if item["field"] == "tunnel.cf_tunnel_token")
    assert token_entry["masked"] is True
    assert token_entry["before"] == "(trống)"
    assert token_entry["after"] == "••••••••••••"
    assert "eyJh" not in str(merged)
    assert "SecretRawValue" not in str(merged)
