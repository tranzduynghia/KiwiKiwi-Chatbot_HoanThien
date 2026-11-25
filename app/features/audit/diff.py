"""Tiện ích so sánh khác biệt (diff) và lọc dữ liệu nhạy cảm trước khi lưu Audit Log."""
from __future__ import annotations

from typing import Any

from app.core.settings import SENSITIVE_KEY_TOKENS


def is_sensitive_field(field_path: str) -> bool:
    """Kiểm tra tên trường có chứa từ khóa nhạy cảm (mật khẩu, token, secret...) hay không."""
    lowered = field_path.lower()
    return any(token in lowered for token in SENSITIVE_KEY_TOKENS)


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
