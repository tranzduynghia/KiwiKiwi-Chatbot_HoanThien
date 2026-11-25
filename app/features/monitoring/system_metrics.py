"""Đọc thông số tài nguyên container/cgroup (RAM, RSS, CPU, số luồng) của tiến trình bot."""
from __future__ import annotations

from typing import Optional

import psutil


def read_memory_info(ram_limit_override_mb: Optional[int] = None) -> dict[str, int]:
    """Đọc mức RAM sử dụng của container (cgroup v1/v2) hoặc máy chủ và RSS của tiến trình Python."""
    proc = psutil.Process()
    rss = proc.memory_info().rss
    used: Optional[int] = None
    limit: Optional[int] = None

    for used_path, limit_path in (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        ("/sys/fs/cgroup/memory/memory.usage_in_bytes", "/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            with open(used_path, encoding="utf-8") as handle:
                used_val = int(handle.read().strip())
            with open(limit_path, encoding="utf-8") as handle:
                raw_limit = handle.read().strip()
            limit_val = None if raw_limit == "max" else int(raw_limit)
            if limit_val and limit_val > 1 << 45:
                limit_val = None
            used, limit = used_val, limit_val
            break
        except Exception:
            continue

    if ram_limit_override_mb:
        limit = int(ram_limit_override_mb) * 1024 * 1024
    if used is None:
        vm = psutil.virtual_memory()
        used = vm.total - vm.available
        limit = limit or vm.total
    limit = limit or psutil.virtual_memory().total
    return {"rss": rss, "used": used, "limit": limit}


def read_process_metrics() -> dict[str, float | int]:
    """Đọc phần trăm CPU hiện tại và số luồng đang chạy của tiến trình bot."""
    proc = psutil.Process()
    return {
        "cpu_percent": round(proc.cpu_percent(None), 1),
        "threads": proc.num_threads(),
    }
