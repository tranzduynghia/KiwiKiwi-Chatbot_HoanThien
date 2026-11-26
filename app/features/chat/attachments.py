"""Kiểm tra dung lượng, định dạng và đọc tệp đính kèm ảnh/PDF từ Discord cho Gemini."""
from __future__ import annotations

import mimetypes

import discord
from google.genai import types

from app.core.errors import UserError

ALLOWED_ATTACHMENT_MIMES = frozenset({
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "application/pdf",
})


async def read_attachment(attachment: discord.Attachment, max_file_mb: float) -> types.Part:
    """Xác thực chặt kích thước (trước và sau khi tải) và kiểu MIME an toàn (chặn SVG/HTML) rồi chuyển thành `types.Part`."""
    max_bytes = int(max(1.0, min(25.0, float(max_file_mb))) * 1024 * 1024)
    if attachment.size > max_bytes:
        raise UserError(f"File quá lớn (tối đa {max_file_mb:g}MB).")
    mime = (
        (attachment.content_type or "").split(";")[0].strip().lower()
        or (mimetypes.guess_type(attachment.filename)[0] or "").lower()
    )
    if mime not in ALLOWED_ATTACHMENT_MIMES:
        raise UserError("Chỉ hỗ trợ file ảnh (png/jpg/webp/gif/heic) hoặc PDF.")
    data = await attachment.read()
    if len(data) > max_bytes:
        raise UserError(f"File quá lớn (tối đa {max_file_mb:g}MB).")
    return types.Part.from_bytes(data=data, mime_type=mime)
