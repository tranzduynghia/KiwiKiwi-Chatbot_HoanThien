"""Các lớp ngoại lệ miền dùng chung trong ứng dụng."""
from __future__ import annotations


class UserError(Exception):
    """Lỗi nghiệp vụ có thể hiển thị trực tiếp cho người dùng Discord."""


class AlreadyExistsError(ValueError):
    """Lỗi báo tài nguyên hoặc tài khoản đã tồn tại trong hệ thống."""
