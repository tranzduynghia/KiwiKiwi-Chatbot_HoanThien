"""Tiện ích so sánh khác biệt (diff) và che tuyệt đối dữ liệu nhạy cảm (Zero-Secret, Zero-Prefix) cho Audit Log."""
from __future__ import annotations

import re
from typing import Any

from app.core.security import STATIC_SECRET_PLACEHOLDER, is_sensitive_key

SAFE_AUDIT_FIELD_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
MAX_CLIENT_DIFF_ENTRIES = 50


def is_sensitive_field(field_path: str) -> bool:
    """Kiểm tra tên trường có chứa từ khóa nhạy cảm hoặc chuỗi băm xác thực hay không."""
    return is_sensitive_key(field_path)


def sanitize_audit_value(value: Any) -> Any:
    """Cắt gọn chuỗi quá dài để bản ghi Audit Log nhẹ và dễ đọc."""
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + f"… ({len(value)} ký tự)"
    return value


def compute_diff(old_obj: Any, new_obj: Any, prefix: str = "") -> list[dict[str, Any]]:
    """So sánh đệ quy hai đối tượng và trả về danh sách trường thay đổi đã ẩn trường nhạy cảm."""
    changes: list[dict[str, Any]] = []
    skip_keys = {"_id", "updated_at", "updated_by"}
    if isinstance(old_obj, dict) and isinstance(new_obj, dict):
        keys = sorted(set(old_obj.keys()) | set(new_obj.keys()))
        for key in keys:
            if not prefix and key in skip_keys:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_sensitive_field(path):
                continue
            old_val = old_obj.get(key)
            new_val = new_obj.get(key)
            if old_val == new_val:
                continue
            if isinstance(old_val, dict) and isinstance(new_val, dict):
                changes.extend(compute_diff(old_val, new_val, path))
            else:
                changes.append({
                    "field": path,
                    "before": sanitize_audit_value(old_val),
                    "after": sanitize_audit_value(new_val),
                })
    elif old_obj != new_obj and not is_sensitive_field(prefix):
        changes.append({
            "field": prefix or "value",
            "before": sanitize_audit_value(old_obj),
            "after": sanitize_audit_value(new_obj),
        })
    return changes


def mask_secret_preview(value: Any) -> str:
    """Che tuyệt đối giá trị bí mật bằng chuỗi giữ chỗ tĩnh (`••••••••••••`) — KHÔNG bao giờ lộ tiền tố, hậu tố hay hash."""
    if value is None or value == "":
        return "(trống)"
    text = str(value).strip()
    if not text or text == "(trống)":
        return "(trống)"
    return STATIC_SECRET_PLACEHOLDER


def merge_client_changes(
    server_changes: list[dict[str, Any]],
    client_changes: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Kết hợp diff tính phía server với bản diff `{field: {old, new}}` từ Modal (chỉ nhận tối đa 50 trường hợp lệ, che tuyệt đối bí mật)."""
    merged = list(server_changes)
    if not isinstance(client_changes, dict):
        return merged

    existing_fields = {str(item.get("field", "")) for item in merged}
    added = 0
    for raw_field, pair in client_changes.items():
        if added >= MAX_CLIENT_DIFF_ENTRIES:
            break
        field = str(raw_field or "").strip()
        if (
            not field
            or field in existing_fields
            or not SAFE_AUDIT_FIELD_RE.fullmatch(field)
            or not isinstance(pair, dict)
        ):
            continue
        old_val = pair.get("old")
        new_val = pair.get("new")
        if old_val == new_val:
            continue
        if is_sensitive_field(field):
            merged.append({
                "field": field,
                "before": mask_secret_preview(old_val),
                "after": mask_secret_preview(new_val),
                "masked": True,
            })
        else:
            merged.append({
                "field": field,
                "before": sanitize_audit_value(old_val),
                "after": sanitize_audit_value(new_val),
            })
        existing_fields.add(field)
        added += 1
    return merged
