"""Thiết lập hệ thống ghi log thống nhất ra stdout cho tiến trình bot và CLI."""
from __future__ import annotations

import logging
import sys


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Cấu hình định dạng log chuẩn và trả về logger gốc của bot."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    return logging.getLogger("gemini-bot")
