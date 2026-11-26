"""Kiểm thử làm sạch comment HTML (Jinja2 `{# ... #}`) và lọc comment JS/CSS tại runtime không sửa mã nguồn trên đĩa."""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.app import PROJECT_ROOT, STATIC_DIR, TEMPLATES_DIR, create_dashboard_app
from app.web.static_assets import strip_css_comments, strip_js_comments


def test_templates_contain_zero_html_comments_and_keep_jinja_comments():
    """Toàn bộ mẫu trong `templates/` không còn `<!--` và vẫn giữ nguyên chú thích Jinja2 `{# ... #}` trên đĩa."""
    html_files = sorted(TEMPLATES_DIR.rglob("*.html"))
    assert html_files, "Phải có ít nhất 1 tệp HTML trong templates/"
    jinja_comment_count = 0
    for path in html_files:
        text = path.read_text(encoding="utf-8")
        assert "<!--" not in text, f"Vẫn còn comment HTML `<!--` trong {path}"
        assert "-->" not in text, f"Vẫn còn comment HTML `-->` trong {path}"
        jinja_comment_count += text.count("{#")
    assert jinja_comment_count >= 40


def test_strip_js_comments_preserves_strings_urls_templates_and_regexes():
    """Bộ lọc comment JS xóa sạch `//` và `/* ... */` nhưng giữ nguyên chuỗi, URL `https://`, template literal và regex."""
    sample = """
    /* Comment đầu tệp nhiều dòng
       không được phép lọt ra trình duyệt */
    import { apiRequest } from "./api.js"; // comment cuối dòng
    const url = "https://cdn.discordapp.com/avatars/1/a.png"; // giữ nguyên https://
    const fakeComment = "/* không phải comment */ // cũng không phải";
    const regex = /^https?:\\/\\/[^/]+\\/api\\/v1$/gi; // giữ nguyên regex
    const ratio = 100 / 4 / 2; // phép chia không bị nhận nhầm thành regex
    const tpl = `Xin chào ${/* comment trong biểu thức */ url} — https://example.com`;
    """
    cleaned = strip_js_comments(sample)
    assert "Comment đầu tệp" not in cleaned
    assert "comment cuối dòng" not in cleaned
    assert "giữ nguyên https://" not in cleaned
    assert "comment trong biểu thức" not in cleaned
    assert 'const url = "https://cdn.discordapp.com/avatars/1/a.png";' in cleaned
    assert 'const fakeComment = "/* không phải comment */ // cũng không phải";' in cleaned
    assert "const regex = /^https?:\\/\\/[^/]+\\/api\\/v1$/gi;" in cleaned
    assert "const ratio = 100 / 4 / 2;" in cleaned
    assert "https://example.com" in cleaned


def test_strip_css_comments_preserves_strings_and_urls():
    """Bộ lọc comment CSS xóa sạch `/* ... */` nhưng giữ nguyên chuỗi và đường dẫn."""
    sample = """
    /* Chú thích hệ thống */
    .card {
      background: #10141c; /* màu nền tối */
      content: "/* giữ nguyên trong chuỗi */";
    }
    """
    cleaned = strip_css_comments(sample)
    assert "Chú thích hệ thống" not in cleaned
    assert "màu nền tối" not in cleaned
    assert 'content: "/* giữ nguyên trong chuỗi */";' in cleaned
    assert "background: #10141c;" in cleaned


@pytest.mark.asyncio
async def test_rendered_pages_and_static_endpoints_strip_all_developer_comments(app_container, fake_bot):
    """Kiểm thử thực tế qua HTTP: `/login`, `/dashboard` và toàn bộ `/static/**/*.js|css` đều sạch comment & chạy hợp lệ."""
    await app_container.auth_service.create_user("admin", "AdminPass123!", role="admin")
    web_app = create_dashboard_app(app_container, get_bot=lambda: fake_bot)

    with TestClient(web_app) as client:
        # 1) Trang /login và /dashboard không chứa comment HTML `<!--`
        login_res = client.get("/login")
        assert login_res.status_code == 200
        assert "<!--" not in login_res.text

        assert client.post("/auth/login", json={"username": "admin", "password": "AdminPass123!"}).status_code == 200
        dash_res = client.get("/dashboard")
        assert dash_res.status_code == 200
        assert "<!--" not in dash_res.text

        # 2) Chặn path traversal trên /static/
        assert client.get("/static/../app/main.py").status_code == 404
        assert client.get("/static/js/does_not_exist.js").status_code == 404

        # 3) Mọi tệp CSS phục vụ qua /static/ đều sạch comment /* ... */ trong khi tệp gốc trên đĩa vẫn giữ comment
        for css_file in sorted(STATIC_DIR.rglob("*.css")):
            rel = css_file.relative_to(STATIC_DIR).as_posix()
            res = client.get(f"/static/{rel}")
            assert res.status_code == 200
            assert "text/css" in res.headers["content-type"]
            assert "ETag" in res.headers
            assert "/*" not in res.text
            assert "*/" not in res.text
            # Xác minh 304 Not Modified hoạt động với ETag
            res_304 = client.get(f"/static/{rel}", headers={"If-None-Match": res.headers["ETag"]})
            assert res_304.status_code == 304
            # Mã nguồn trên đĩa vẫn còn comment phát triển
            assert "/*" in css_file.read_text(encoding="utf-8")

        # 4) Mọi tệp JS phục vụ qua /static/ đều sạch comment, hợp lệ cú pháp với `node --check` và không sửa tệp gốc
        comment_line_re = re.compile(r"^\s*(//|/\*|\*)")
        with tempfile.TemporaryDirectory() as tmpdir:
            for js_file in sorted(STATIC_DIR.rglob("*.js")):
                rel = js_file.relative_to(STATIC_DIR).as_posix()
                res = client.get(f"/static/{rel}")
                assert res.status_code == 200
                assert "javascript" in res.headers["content-type"]
                assert "/*" not in res.text, f"Còn comment block trong /static/{rel}"
                for lineno, line in enumerate(res.text.splitlines(), 1):
                    assert not comment_line_re.match(line), f"Còn dòng comment ở /static/{rel}:{lineno}: {line}"

                # Kiểm tra cú pháp JS đã lọc bằng `node --check` (module mode)
                tmp_js = Path(tmpdir) / js_file.name.replace(".js", ".mjs")
                tmp_js.write_text(res.text, encoding="utf-8")
                check = subprocess.run(
                    ["node", "--check", str(tmp_js)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                assert check.returncode == 0, f"Lỗi cú pháp JS sau khi lọc /static/{rel}: {check.stderr}"

                # Đảm bảo mã nguồn trên đĩa vẫn còn comment phát triển
                disk_text = js_file.read_text(encoding="utf-8")
                assert ("/*" in disk_text) or ("//" in disk_text), f"Tệp nguồn {rel} bị mất comment trên đĩa"

            # Đảm bảo import ES module tương đối được gắn tự động `?v=<ASSET_VERSION>` khi phục vụ runtime
            dash_js = client.get("/static/js/dashboard.js").text
            assert "./features/config/tunnelCard.js?v=" in dash_js
            confirm_js = client.get("/static/js/core/confirmSave.js").text
            assert "./modalTransition.js?v=" in confirm_js
