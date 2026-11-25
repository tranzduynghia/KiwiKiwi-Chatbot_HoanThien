"""Các schema Pydantic cho đăng nhập và quản trị tài khoản Dashboard."""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginPayload(BaseModel):
    """Payload đăng nhập bằng tên đăng nhập và mật khẩu."""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class CreateUserPayload(BaseModel):
    """Payload tạo tài khoản quản trị hoặc vận hành mới."""

    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=128)
    role: str = Field(default="admin")


class ChangePasswordPayload(BaseModel):
    """Payload đổi mật khẩu cho một tài khoản Dashboard hiện có."""

    password: str = Field(..., min_length=6, max_length=128)
