"""Tác vụ nền bất đồng bộ xóa bộ đệm Cloudflare CDN Edge cho 5 trang Pháp lý công khai khi Admin lưu thay đổi."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from app.features.legal.defaults import LEGAL_DOC_KEYS, PUBLIC_SITE_ORIGIN

log = logging.getLogger("gemini-bot.legal.cdn")

CLOUDFLARE_PURGE_TIMEOUT_SECONDS = 5.0


def build_legal_public_urls(origin: Optional[str] = None) -> list[str]:
    """Tạo danh sách 5 URL công khai cần xóa cache trên Cloudflare Edge."""
    base = (origin or os.getenv("DASHBOARD_PUBLIC_URL") or PUBLIC_SITE_ORIGIN).strip().rstrip("/")
    return [f"{base}/{key}" for key in LEGAL_DOC_KEYS]


async def purge_legal_pages_edge_cache(
    *,
    origin: Optional[str] = None,
    zone_id: Optional[str] = None,
    api_token: Optional[str] = None,
    http_client: Optional[Any] = None,
) -> dict[str, Any]:
    """Gọi Cloudflare Purge Cache API cho 5 URL pháp lý nếu đã cấu hình `CF_ZONE_ID` và `CF_API_TOKEN`."""
    resolved_zone = (zone_id if zone_id is not None else os.getenv("CF_ZONE_ID") or "").strip()
    resolved_token = (api_token if api_token is not None else os.getenv("CF_API_TOKEN") or "").strip()
    urls = build_legal_public_urls(origin)

    if not resolved_zone or not resolved_token:
        return {"purged": False, "skipped": True, "reason": "cf_credentials_not_configured", "urls": urls}

    endpoint = f"https://api.cloudflare.com/client/v4/zones/{resolved_zone}/purge_cache"
    headers = {
        "Authorization": f"Bearer {resolved_token}",
        "Content-Type": "application/json",
    }
    payload = {"files": urls}

    try:
        if http_client is not None:
            resp = await http_client.post(endpoint, json=payload, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=CLOUDFLARE_PURGE_TIMEOUT_SECONDS) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)

        ok = 200 <= getattr(resp, "status_code", 0) < 300
        if ok:
            log.info("Đã xóa bộ đệm Cloudflare Edge cho %d trang pháp lý.", len(urls))
            return {"purged": True, "status_code": resp.status_code, "urls": urls}

        log.warning("Cloudflare Purge Cache trả mã HTTP %s (bỏ qua không ảnh hưởng lưu DB).", getattr(resp, "status_code", "?"))
        return {"purged": False, "status_code": getattr(resp, "status_code", 0), "urls": urls}
    except Exception as exc:
        log.warning("Không gọi được Cloudflare Purge Cache (%s) — bỏ qua không chặn lưu CMS.", type(exc).__name__)
        return {"purged": False, "error": type(exc).__name__, "urls": urls}


def schedule_legal_cache_purge(*, origin: Optional[str] = None) -> Optional[asyncio.Task]:
    """Lên lịch xóa cache Cloudflare Edge bất đồng bộ (non-blocking) khi có vòng lặp asyncio đang chạy."""
    zone_id = (os.getenv("CF_ZONE_ID") or "").strip()
    api_token = (os.getenv("CF_API_TOKEN") or "").strip()
    if not zone_id or not api_token:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(purge_legal_pages_edge_cache(origin=origin, zone_id=zone_id, api_token=api_token))
