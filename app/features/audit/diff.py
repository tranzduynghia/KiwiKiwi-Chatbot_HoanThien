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


def mask_secret_preview(value: Any) -> str:
    """Che giá trị nhạy cảm dạng tiền tố ngắn + dấu chấm (VD: `eyJh•••••`) để hiển thị an toàn trong Audit Log."""
    if value is None or value == "":
        return "(trống)"
    text = str(value).strip()
    if not text:
        return "(trống)"
    if "••••" in text or text.startswith("("):
        return text[:40]
    if len(text) <= 4:
        return "•••••"
    return f"{text[:4]}•••••"


def merge_client_changes(
    server_changes: list[dict[str, Any]],
    client_changes: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Kết hợp diff tính phía server với bản diff `{field: {old, new}}` do Admin xác nhận trên Modal (tự che bí mật)."""
    merged = list(server_changes)
    if not isinstance(client_changes, dict):
        return merged

    existing_fields = {str(item.get("field", "")) for item in merged}
    for raw_field, pair in client_changes.items():
        field = str(raw_field or "").strip()
        if not field or field in existing_fields or not isinstance(pair, dict):
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
    return merged
