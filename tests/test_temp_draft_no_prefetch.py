"""Kiểm thử trang nháp /temp, cơ chế chặn prefetch/prerender, xác thực bắt buộc, đẩy sang 5 trang công khai và header bảo mật."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.security import NO_INDEX_ROBOTS_TAG, NO_PREFETCH_CACHE_CONTROL
from app.web.app import create_dashboard_app

PREFETCH_HEADER_CASES = [
    {"Sec-Purpose": "prefetch"},
    {"Sec-Purpose": "prerender"},
    {"Purpose": "prefetch"},
    {"X-Purpose": "preview"},
    {"X-Moz": "prefetch"},
]


def _assert_no_store_noindex(resp) -> None:
    assert resp.headers.get("cache-control") == NO_PREFETCH_CACHE_CONTROL
    assert resp.headers.get("x-robots-tag") == NO_INDEX_ROBOTS_TAG


@pytest.mark.asyncio
async def test_temp_blocks_unauthenticated_and_never_queries_mongodb(app_container, fake_db, fake_bot, monkeypatch):
    """Chưa đăng nhập gọi /temp hoặc /api/legal/temp* bị chặn 401 ngay lập tức, tuyệt đối không truy vấn MongoDB."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.legal_doc_repo.attach_db(fake_db)
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    called = {"get": 0, "save": 0}
    orig_get = app_container.legal_doc_repo.get_document
    orig_save = app_container.legal_doc_repo.save_document

    async def spy_get(*args, **kwargs):
        called["get"] += 1
        return await orig_get(*args, **kwargs)

    async def spy_save(*args, **kwargs):
        called["save"] += 1
        return await orig_save(*args, **kwargs)

    monkeypatch.setattr(app_container.legal_doc_repo, "get_document", spy_get)
    monkeypatch.setattr(app_container.legal_doc_repo, "save_document", spy_save)

    with TestClient(web_app) as client:
        for method, url, payload in (
            ("GET", "/temp", None),
            ("GET", "/api/legal/temp", None),
            ("PUT", "/api/legal/temp", {"title": "Nháp", "subtitle": "", "body": "<p>Nội dung nháp</p>"}),
            ("POST", "/api/legal/temp/push", {"target_doc_key": "terms"}),
        ):
            res = client.request(method, url, json=payload) if payload is not None else client.request(method, url)
            assert res.status_code == 401
            _assert_no_store_noindex(res)

    assert called["get"] == 0
    assert called["save"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("prefetch_headers", PREFETCH_HEADER_CASES)
async def test_temp_blocks_prefetch_headers_even_when_authenticated(
    app_container,
    fake_db,
    fake_bot,
    monkeypatch,
    prefetch_headers,
):
    """Mọi request tới /temp hoặc API /temp có header prefetch/prerender/preview đều bị chặn 403 và không chạm MongoDB."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.legal_doc_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    called = {"get": 0, "save": 0}
    orig_get = app_container.legal_doc_repo.get_document
    orig_save = app_container.legal_doc_repo.save_document

    async def spy_get(*args, **kwargs):
        called["get"] += 1
        return await orig_get(*args, **kwargs)

    async def spy_save(*args, **kwargs):
        called["save"] += 1
        return await orig_save(*args, **kwargs)

    monkeypatch.setattr(app_container.legal_doc_repo, "get_document", spy_get)
    monkeypatch.setattr(app_container.legal_doc_repo, "save_document", spy_save)

    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        for method, url, payload in (
            ("GET", "/temp", None),
            ("GET", "/api/legal/temp", None),
            ("PUT", "/api/legal/temp", {"title": "Nháp", "subtitle": "", "body": "<p>Nội dung nháp</p>"}),
            ("POST", "/api/legal/temp/push", {"target_doc_key": "terms"}),
        ):
            res = (
                client.request(method, url, json=payload, headers=prefetch_headers)
                if payload is not None
                else client.request(method, url, headers=prefetch_headers)
            )
            assert res.status_code == 403
            _assert_no_store_noindex(res)

    assert called["get"] == 0
    assert called["save"] == 0


@pytest.mark.asyncio
async def test_temp_save_read_hidden_from_public_and_push_to_page(app_container, fake_db, fake_bot):
    """Admin lưu /temp trên MongoDB, ẩn hoàn toàn khỏi /toslinks, xem /temp khi đã xác thực và đẩy sang /terms."""
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.legal_doc_repo.attach_db(fake_db)
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    draft_marker = "NOI_DUNG_NHAP_BI_MAT_998877"
    with TestClient(web_app) as client:
        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200

        # Lưu nháp /temp
        put_res = client.put(
            "/api/legal/temp",
            json={
                "title": "Bản nháp Điều khoản mới",
                "subtitle": "Chờ duyệt nội bộ",
                "body": f"<p><span style=\"font-size: 18px\">{draft_marker}</span></p><script>alert(1)</script>",
            },
        )
        assert put_res.status_code == 200
        _assert_no_store_noindex(put_res)
        assert draft_marker in put_res.json()["doc"]["body"]
        assert "<script" not in put_res.json()["doc"]["body"]

        # Đọc lại /api/legal/temp
        get_api = client.get("/api/legal/temp")
        assert get_api.status_code == 200
        _assert_no_store_noindex(get_api)
        assert draft_marker in get_api.json()["doc"]["body"]

        # /api/legal?doc_key=temp phải bị từ chối
        assert client.get("/api/legal", params={"doc_key": "temp"}).status_code == 400

        # Trang HTML /temp khi đã đăng nhập
        temp_page = client.get("/temp")
        assert temp_page.status_code == 200
        _assert_no_store_noindex(temp_page)
        assert '<meta name="robots" content="noindex, nofollow"' in temp_page.text
        assert draft_marker in temp_page.text
        assert 'rel="prefetch"' not in temp_page.text

        # /toslinks tuyệt đối không lộ /temp hay nội dung nháp
        toslinks_page = client.get("/toslinks")
        assert toslinks_page.status_code == 200
        assert "/temp" not in toslinks_page.text
        assert draft_marker not in toslinks_page.text

        # Đẩy sang /terms
        push_res = client.post(
            "/api/legal/temp/push",
            json={
                "target_doc_key": "terms",
                "title": "Điều khoản đã đẩy từ /temp",
                "subtitle": "Phiên bản chính thức",
                "body": f"<p>{draft_marker}</p>",
                "active_tos_version": "v1.2",
            },
        )
        assert push_res.status_code == 200
        _assert_no_store_noindex(push_res)
        assert push_res.json()["pushed_to"] == "terms"

        terms_page = client.get("/terms")
        assert terms_page.status_code == 200
        assert draft_marker in terms_page.text
        assert "v1.2" in terms_page.text


def test_temp_templates_have_no_prefetch_links_and_only_jinja_comments():
    """Template legal_cms_tab.html và legal_page.html không gắn rel=prefetch/prerender và không dùng comment HTML."""
    root = Path(__file__).resolve().parents[1]
    for rel_path in ("templates/partials/legal_cms_tab.html", "templates/legal_page.html"):
        text = (root / rel_path).read_text(encoding="utf-8")
        assert "<!--" not in text
        assert 'rel="prefetch"' not in text.lower()
        assert 'rel="prerender"' not in text.lower()
