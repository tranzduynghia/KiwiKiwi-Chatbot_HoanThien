"""Kiểm thử nâng cấp Rich-Text Sanitizer (10–72px, chống XSS, Google Fonts CDN link tự động trên 5 trang pháp lý)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.features.legal.defaults import LEGAL_DOC_KEYS
from app.features.legal.sanitizer import (
    build_google_fonts_stylesheet_urls,
    sanitize_inline_style,
    sanitize_legal_html,
)
from app.web.app import create_dashboard_app


def test_legal_sanitizer_supports_10_to_72px_and_blocks_xss():
    """Hỗ trợ cỡ chữ 10px–72px, chặn out-of-bounds, script, event handlers, unbalanced quotes và thẻ rỗng."""
    dirty = (
        '<p onclick="alert(1)" style="font-size: 64px; font-family: \'Be Vietnam Pro\', sans-serif; '
        'position:fixed; background: url(javascript:alert(2))">Hello <b>World</b></p>'
        '<script>alert(1)</script>'
        '<span style="font-size: 73px">TooBig</span>'
        '<span style="font-size: 9px">TooSmall</span>'
        '<span style="font-family: \'Evil;url(x)\'">BadFont</span>'
        '<strong>   </strong><b></b><span></span>'
    )
    clean = sanitize_legal_html(dirty)
    assert "<script" not in clean
    assert "onclick" not in clean
    assert "position:fixed" not in clean
    assert "73px" not in clean
    assert "9px" not in clean
    assert "Evil" not in clean
    assert "<b></b>" not in clean
    assert "<span></span>" not in clean
    assert "font-size: 64px" in clean
    assert "font-family: 'Be Vietnam Pro', sans-serif" in clean

    assert sanitize_inline_style("font-size: 10px") == "font-size: 10px"
    assert sanitize_inline_style("font-size: 72px") == "font-size: 72px"
    assert sanitize_inline_style("font-size: 9px") == ""
    assert sanitize_inline_style("font-size: 73px") == ""


@pytest.mark.asyncio
async def test_public_5_legal_pages_inject_google_fonts_link(app_container, fake_db, fake_bot):
    """Lưu nội dung Rich-Text có Google Font qua PUT /api/legal -> 5 trang pháp lý công khai tự động chèn <link> Google Fonts."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.legal_doc_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        for doc_key in LEGAL_DOC_KEYS:
            res = client.put(
                "/api/legal",
                json={
                    "doc_key": doc_key,
                    "title": f"Tiêu đề {doc_key}",
                    "subtitle": f"Phụ đề {doc_key}",
                    "body": (
                        '<p><span style="font-family: \'Playfair Display\', serif; font-size: 28px">'
                        f'Nội dung {doc_key}</span><script>alert(1)</script></p>'
                    ),
                    "active_tos_version": "v1.0",
                },
            )
            assert res.status_code == 200

        for doc_key in LEGAL_DOC_KEYS:
            pub = client.get(f"/{doc_key}")
            assert pub.status_code == 200
            assert f"Nội dung {doc_key}" in pub.text
            assert "<script>alert(1)</script>" not in pub.text
            assert 'href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&amp;display=swap"' in pub.text or (
                'href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;500;600;700&display=swap"' in pub.text
            )

    assert build_google_fonts_stylesheet_urls('<p style="font-family: Georgia, serif">Built-in</p>') == []
