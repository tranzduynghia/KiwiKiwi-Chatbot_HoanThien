"""Kiểm tra dung lượng, định dạng và đọc tệp đính kèm ảnh/PDF từ Discord cho Gemini."""
from __future__ import annotations

import mimetypes

import discord
from google.genai import types

from app.core.errors import UserError


async def read_attachment(attachment: discord.Attachment, max_file_mb: float) -> types.Part:
    """Xác thực kích thước và kiểu MIME của tệp đính kèm rồi chuyển thành `types.Part`."""
    if attachment.size > max_file_mb * 1024 * 1024:
        raise UserError(f"File quá lớn (tối đa {max_file_mb:g}MB).")
    mime = (attachment.content_type or "").split(";")[0] or mimetypes.guess_type(attachment.filename)[0] or ""
    if not (mime.startswith("image/") or mime == "application/pdf"):
        raise UserError("Chỉ hỗ trợ file ảnh (png/jpg/webp...) hoặc PDF.")
    data = await attachment.read()
    return types.Part.from_bytes(data=data, mime_type=mime)
