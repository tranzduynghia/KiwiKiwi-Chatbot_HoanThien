"""Kiểm thử toàn diện cho Module A (3-Mode Theme), Module B (Legal/ToS CMS, Public Pages & Consent Gate) và Module C (Admin Login History)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from fastapi.testclient import TestClient

from app.bot.client import DndCommandTree
from app.bot.tos_gate import TOS_CONSENT_VIEW_TIMEOUT_SECONDS, TosConsentGate, TosConsentView
from app.core.timezone import format_exact_timestamp, parse_user_agent_summary, resolve_request_timezone
from app.features.auth.login_history import LOGIN_HISTORY_TTL_SECONDS
from app.features.legal.sanitizer import sanitize_legal_html
from app.web.app import create_dashboard_app


def test_stdlib_html_sanitizer_allows_safe_rich_text_and_strips_xss():
    """Bộ lọc thuần html.parser giữ nguyên thẻ/kiểu hợp lệ và triệt tiêu toàn bộ script, on*, iframe, object, javascript:, CSS độc."""
    dirty_html = (
        '<h2 style="font-family: Georgia, serif; font-size: 18px; text-align: center; color: #f43f5e; background: url(javascript:alert(1)); position: fixed;">'
        'Tiêu đề An toàn</h2>'
        '<p onclick="alert(1)">Đoạn văn <strong>in đậm</strong> <em>nghiêng</em> <u>gạch chân</u> <s>gạch ngang</s>.</p>'
        '<script>alert("xss")</script>'
        '<iframe src="https://evil.example"></iframe>'
        '<object data="evil.swf"></object>'
        '<img src="x" onerror="alert(2)" />'
        '<a href="javascript:alert(3)">Link xấu</a> '
        '<a href="/terms">Tuyến nội bộ</a> '
        '<a href="https://kiwikiwi.dpdns.org/policies">Liên kết ngoài</a>'
    )
    clean = sanitize_legal_html(dirty_html)

    # Triệt tiêu hoàn toàn các vector XSS
    assert "<script" not in clean.lower()
    assert "alert(" not in clean
    assert "<iframe" not in clean.lower()
    assert "<object" not in clean.lower()
    assert "<img" not in clean.lower()
    assert "onclick" not in clean.lower()
    assert "onerror" not in clean.lower()
    assert "javascript:" not in clean.lower()
    assert "background" not in clean.lower()
    assert "position" not in clean.lower()

    # Giữ nguyên thẻ & thuộc tính an toàn + tự động gắn rel="noopener noreferrer" cho https://
    assert "<h2 " in clean
    assert "font-family: Georgia, serif" in clean
    assert "font-size: 18px" in clean
    assert "text-align: center" in clean
    assert "color: #f43f5e" in clean
    assert "<strong>in đậm</strong>" in clean
    assert '<a href="/terms">Tuyến nội bộ</a>' in clean
    assert 'href="https://kiwikiwi.dpdns.org/policies"' in clean
    assert 'rel="noopener noreferrer"' in clean


class FakeInteractionResponse:
    """Mô phỏng `interaction.response` hỗ trợ `send_message` và `edit_message`."""

    def __init__(self) -> None:
        self._done = False
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, content: str, *, view: Any = None, ephemeral: bool = False) -> None:
        self._done = True
        self.sent.append({"content": content, "view": view, "ephemeral": ephemeral})

    async def edit_message(self, *, content: str = "", view: Any = None) -> None:
        self._done = True
        self.edited.append({"content": content, "view": view})


def _make_slash_interaction(user_id: int, command_name: str = "ask", display_name: str = "Discord User") -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, name=display_name, display_name=display_name),
        command=SimpleNamespace(name=command_name, qualified_name=command_name),
        data={"name": command_name},
        type=discord.InteractionType.application_command,
        channel_id=12345,
        response=FakeInteractionResponse(),
        followup=SimpleNamespace(send=lambda *a, **kw: None),
    )


def test_theme_css_tokens_and_zero_fouc_loader():
    """CSS định nghĩa đủ 3 giao diện (normal / dark / light) với biến ngữ nghĩa và file themeInit.js tồn tại."""
    root = Path(__file__).resolve().parents[1]
    css = (root / "static/css/dashboard.css").read_text(encoding="utf-8")
    for token in (
        'html[data-theme="normal"]',
        'html[data-theme="dark"]',
        'html[data-theme="light"]',
        "--bg-base",
        "--bg-surface",
        "--bg-elevated",
        "--border-subtle",
        "--text-primary",
        "--text-secondary",
        "--accent-primary",
        "--modal-backdrop",
    ):
        assert token in css

    init_js = (root / "static/js/core/themeInit.js").read_text(encoding="utf-8")
    assert "kiwi_theme" in init_js
    assert "dataset.theme" in init_js


def test_timezone_resolution_exact_second_formatting_and_ua_parser():
    """Phân giải CF-Timezone chính xác, định dạng HH:mm:ss DD/MM/YYYY tới từng giây và phân tích User-Agent gọn gàng."""
    sample_utc = datetime(2026, 11, 27, 6, 15, 42, tzinfo=timezone.utc)

    tz_name_vn, tz_vn = resolve_request_timezone({"CF-Timezone": "Asia/Ho_Chi_Minh"})
    assert tz_name_vn == "Asia/Ho_Chi_Minh"
    assert format_exact_timestamp(sample_utc, tz_vn) == "13:15:42 27/11/2026"

    tz_name_ny, tz_ny = resolve_request_timezone({"CF-Timezone": "America/New_York"})
    assert tz_name_ny == "America/New_York"
    assert format_exact_timestamp(sample_utc, tz_ny) == "01:15:42 27/11/2026"

    # Header không hợp lệ -> fallback về múi giờ trình duyệt, nếu vẫn sai -> Asia/Ho_Chi_Minh
    tz_fallback, _ = resolve_request_timezone({"CF-Timezone": "Invalid/Zone"}, fallback_tz="Asia/Tokyo")
    assert tz_fallback == "Asia/Tokyo"
    tz_default, _ = resolve_request_timezone({"CF-Timezone": "Invalid/Zone"}, fallback_tz="Bad/Tz")
    assert tz_default == "Asia/Ho_Chi_Minh"

    assert parse_user_agent_summary(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
    ) == "Chrome · Windows"
    assert parse_user_agent_summary(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15"
    ) == "Safari · macOS"


@pytest.mark.asyncio
async def test_public_legal_routes_cms_version_bump_and_zero_html_comments(
    monkeypatch,
    app_container,
    fake_db,
    fake_bot,
):
    """5 trang pháp lý công khai (/toslinks..guidelines) trả HTTP 200 không cần đăng nhập, đọc theo yêu cầu từ MongoDB, chống XSS và không rò comment HTML."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "AdminPassword123!")
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.legal_doc_repo.attach_db(fake_db)
    await app_container.tos_acceptance_repo.attach_db(fake_db)
    await app_container.login_history_repo.attach_db(fake_db)

    # Kiểm chứng 5 trang đã được tự động seed trong `legal_documents` và KHÔNG đệm HTML trong `bot_config` RAM
    assert len(fake_db["legal_documents"].docs) == 5
    assert "docs" not in (app_container.config_repo.current_config.get("legal") or {})

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    client = TestClient(web_app)

    # 1) Kiểm tra cả 5 trang công khai trước khi đăng nhập (kèm CF-Timezone)
    for route in ("/toslinks", "/legal", "/terms", "/policies", "/guidelines"):
        res = client.get(route, headers={"CF-Timezone": "Asia/Ho_Chi_Minh"})
        assert res.status_code == 200
        html = res.text
        assert "v1.0" in html
        assert "Asia/Ho_Chi_Minh (UTC+7)" in html
        assert "themeInit.js" in html
        assert "<!--" not in html

    # Trang /login cũng hiển thị link pháp lý và không có comment HTML
    login_html = client.get("/login").text
    assert "/terms" in login_html and "/policies" in login_html and "/guidelines" in login_html and "/legal" in login_html
    assert "<!--" not in login_html

    # 2) Đăng nhập Admin và cập nhật /terms + nâng phiên bản v1.0 -> v1.1 (kèm payload XSS để kiểm tra lọc allowlist)
    login_res = client.post("/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    assert login_res.status_code == 200

    # Phiên bản sai định dạng phải bị từ chối 400
    bad_ver = client.put(
        "/api/legal",
        json={
            "doc_key": "terms",
            "title": "Điều khoản mới",
            "subtitle": "Bản thử nghiệm",
            "body": "<p>Nội dung điều khoản đủ dài trên mười ký tự.</p>",
            "active_tos_version": "invalid-ver",
        },
    )
    assert bad_ver.status_code == 400

    # Cập nhật hợp lệ -> nâng lên v1.1, lọc sạch <script>/onclick và ghi Audit Log
    ok_res = client.put(
        "/api/legal",
        json={
            "doc_key": "terms",
            "title": "Điều khoản Dịch vụ Cập nhật",
            "subtitle": "Hiệu lực từ bản v1.1",
            "body": '<p onclick="evil()">Điều khoản <strong>mới</strong> áp dụng ngay.</p><script>alert("xss")</script>',
            "active_tos_version": "v1.1",
        },
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["active_tos_version"] == "v1.1"
    assert app_container.legal_service.active_tos_version == "v1.1"

    # Kiểm tra trang công khai /terms đã cập nhật v1.1, giữ <strong> và loại bỏ hoàn toàn <script>/onclick
    terms_html = client.get("/terms", headers={"CF-Timezone": "Asia/Tokyo"}).text
    assert "v1.1" in terms_html
    assert "Điều khoản Dịch vụ Cập nhật" in terms_html
    assert "Asia/Tokyo (UTC+9)" in terms_html
    assert "<strong>mới</strong>" in terms_html
    assert "<script>" not in terms_html
    assert "onclick" not in terms_html
    assert "<!--" not in terms_html

    # Kiểm tra Audit Log đã lưu dưới chuyên mục legal
    legal_audits = [d for d in fake_db["audit_logs"].docs if d.get("section") == "legal"]
    assert len(legal_audits) >= 1

    # Kiểm tra /dashboard render sạch comment HTML và có trình soạn thảo Rich-Text
    dash_html = client.get("/dashboard").text
    assert 'id="tab-legal"' in dash_html
    assert 'id="legal-rte-editor"' in dash_html
    assert 'id="tos-consent-modal"' in dash_html
    assert 'id="login-history-tbody"' in dash_html
    assert "<!--" not in dash_html


@pytest.mark.asyncio
async def test_discord_tos_consent_gate_version_bump_and_directory_timezone(
    monkeypatch,
    app_container,
    fake_db,
    fake_bot,
):
    """Discord ToS Gate chặn /ask trước Gemini khi chưa đồng ý, cho qua O(1) sau khi đồng ý, và yêu cầu đồng ý lại khi nâng version."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "AdminPassword123!")
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.tos_acceptance_repo.attach_db(fake_db)

    discord_client = discord.Client(intents=discord.Intents.default())
    tree = DndCommandTree(
        discord_client,
        maintenance_gate=app_container.maintenance_gate,
        tos_gate=app_container.tos_gate,
    )

    # 1) User lần đầu gọi /ask -> bị chặn kèm prompt ephemeral có link kiwikiwi.dpdns.org/terms và TosConsentView(timeout=60)
    inter1 = _make_slash_interaction(user_id=888001, command_name="ask", display_name="Kiwi Member")
    allowed1 = await tree.interaction_check(inter1)
    assert allowed1 is False
    assert len(inter1.response.sent) == 1
    sent = inter1.response.sent[0]
    assert sent["ephemeral"] is True
    assert "https://kiwikiwi.dpdns.org/terms" in sent["content"]
    assert "https://kiwikiwi.dpdns.org/policies" in sent["content"]
    assert "https://kiwikiwi.dpdns.org/guidelines" in sent["content"]
    view = sent["view"]
    assert isinstance(view, TosConsentView)
    assert view.timeout == TOS_CONSENT_VIEW_TIMEOUT_SECONDS

    # 2) User nhấn nút "Tôi đồng ý" -> lưu MongoDB + RAM cache, dừng View chống rò rỉ RAM
    btn_inter = _make_slash_interaction(user_id=888001, command_name="ask", display_name="Kiwi Member")
    accept_btn = view.children[0]
    await accept_btn.callback(btn_inter)
    assert view.is_finished() is True
    assert app_container.legal_service.has_accepted_current_tos_fast(888001) is True

    # 3) Gọi lại /ask -> được phép đi qua ngay (0 độ trễ DB)
    inter2 = _make_slash_interaction(user_id=888001, command_name="ask", display_name="Kiwi Member")
    assert await tree.interaction_check(inter2) is True
    assert inter2.response.sent == []

    # 4) Admin nâng phiên bản ToS lên v1.2 -> user lập tức phải xác nhận lại (thông báo cập nhật phiên bản)
    await app_container.legal_service.update_document(
        doc_key="terms",
        title="Điều khoản v1.2",
        subtitle="Bản nâng cấp",
        body="Nội dung điều khoản phiên bản v1.2 dài hơn mười ký tự.",
        active_tos_version="v1.2",
        actor="admin",
    )
    inter3 = _make_slash_interaction(user_id=888001, command_name="ask", display_name="Kiwi Member")
    assert await tree.interaction_check(inter3) is False
    assert "v1.2" in inter3.response.sent[0]["content"]
    assert "v1.0" in inter3.response.sent[0]["content"]
    # Kiểm tra on_timeout dừng View sạch
    await inter3.response.sent[0]["view"].on_timeout()
    assert inter3.response.sent[0]["view"].is_finished() is True

    # 5) Kiểm tra API Danh sách chấp thuận ToS trả về thời gian chính xác từng giây theo CF-Timezone
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    client = TestClient(web_app)
    client.post("/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    acc_res = client.get(
        "/api/legal/acceptances?limit=15&q=888001",
        headers={"CF-Timezone": "Asia/Ho_Chi_Minh"},
    )
    assert acc_res.status_code == 200
    acc_data = acc_res.json()
    assert acc_data["timezone"] == "Asia/Ho_Chi_Minh"
    assert acc_data["timezone_label"] == "Asia/Ho_Chi_Minh (UTC+7)"
    assert acc_data["total"] == 1
    row = acc_data["items"][0]
    assert row["discord_user_id"] == "888001"
    assert row["tos_accepted_version"] == "v1.0"
    assert row["is_current_version"] is False
    assert row["consent_source"] == "discord"
    assert len(row["tos_accepted_at_formatted"]) == 19  # HH:mm:ss DD/MM/YYYY


@pytest.mark.asyncio
async def test_admin_login_history_records_success_and_failed_without_password_leak(
    monkeypatch,
    app_container,
    fake_db,
    fake_bot,
):
    """Ghi nhận cả đăng nhập thành công & thất bại kèm TTL 90 ngày, múi giờ CF-Timezone và tuyệt đối KHÔNG rò mật khẩu."""
    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INITIAL_ADMIN_PASSWORD", "SuperSecretPass99!")
    await app_container.sync_coordinator.attach_db(fake_db)
    await app_container.login_history_repo.attach_db(fake_db)

    # Xác nhận đã tạo TTL index 90 ngày trên created_at
    ttl_indexes = fake_db["admin_login_history"].indexes
    assert any(meta.get("expireAfterSeconds") == LOGIN_HISTORY_TTL_SECONDS for meta in ttl_indexes.values())

    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)
    client = TestClient(web_app)

    cf_headers = {
        "CF-Connecting-IP": "203.0.113.45",
        "CF-IPCountry": "VN",
        "CF-Timezone": "Asia/Ho_Chi_Minh",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36",
    }

    # 1) Đăng nhập thất bại với mật khẩu sai
    bad_password = "WrongSecretPassword!123"
    fail_res = client.post(
        "/auth/login",
        json={"username": "admin", "password": bad_password},
        headers=cf_headers,
    )
    assert fail_res.status_code == 401

    # 2) Đăng nhập thành công
    ok_res = client.post(
        "/auth/login",
        json={"username": "admin", "password": "SuperSecretPass99!"},
        headers=cf_headers,
    )
    assert ok_res.status_code == 200

    # Kiểm tra raw docs trong MongoDB không bao giờ chứa mật khẩu hay password_hash
    raw_docs = fake_db["admin_login_history"].docs
    assert len(raw_docs) == 2
    serialized_db = str(raw_docs)
    assert bad_password not in serialized_db
    assert "SuperSecretPass99!" not in serialized_db
    assert "password_hash" not in serialized_db

    # 3) Truy vấn API /api/auth/login-history với lọc trạng thái và múi giờ CF-Timezone
    hist_all = client.get("/api/auth/login-history?limit=15&status=all", headers={"CF-Timezone": "Asia/Ho_Chi_Minh"})
    assert hist_all.status_code == 200
    body = hist_all.json()
    assert body["timezone_label"] == "Asia/Ho_Chi_Minh (UTC+7)"
    assert body["total"] == 2
    assert body["items"][0]["status"] == "success"
    assert body["items"][0]["country_code"] == "VN"
    assert body["items"][0]["device_summary"] == "Chrome · Windows"
    assert len(body["items"][0]["created_at_formatted"]) == 19

    hist_failed = client.get("/api/auth/login-history?limit=15&status=failed", headers={"CF-Timezone": "America/New_York"})
    assert hist_failed.status_code == 200
    failed_body = hist_failed.json()
    assert failed_body["timezone"] == "America/New_York"
    assert failed_body["total"] == 1
    assert failed_body["items"][0]["status"] == "failed"
    assert failed_body["items"][0]["failure_reason"] == "invalid_credentials"
    assert bad_password not in hist_failed.text
