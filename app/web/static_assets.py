"""Bộ xử lý phục vụ tài nguyên tĩnh `/static/` có lọc bỏ comment JS/CSS tại runtime (giữ nguyên mã nguồn trên đĩa)."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

# Từ khóa JavaScript mà dấu `/` đứng ngay sau sẽ bắt đầu biểu thức chính quy (regex literal) thay vì phép chia
_JS_REGEX_PRECEDING_KEYWORDS = frozenset({
    "return",
    "throw",
    "case",
    "in",
    "of",
    "typeof",
    "void",
    "delete",
    "yield",
    "await",
    "else",
    "do",
    "new",
    "instanceof",
})

# Ký tự đơn (toán tử/dấu phân cách) mà dấu `/` đứng ngay sau sẽ bắt đầu regex literal
_JS_REGEX_PRECEDING_CHARS = frozenset("=([{,;:?!&|^~+-*%<>")

_MEDIA_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _cleanup_blank_lines(text: str) -> str:
    """Dọn gọn khoảng trắng cuối dòng và các dòng trống thừa sau khi cắt comment mà vẫn giữ nguyên cấu trúc xuống dòng."""
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    prev_blank = True
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned.append("")
                prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    return ("\n".join(cleaned) + "\n") if cleaned else ""


def strip_css_comments(source: str) -> str:
    """Loại bỏ an toàn mọi comment `/* ... */` trong CSS mà không làm hỏng chuỗi dấu nháy đơn/kép hoặc URL."""
    out: list[str] = []
    i = 0
    n = len(source)
    in_quote: str | None = None

    while i < n:
        ch = source[i]
        if in_quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(source[i + 1])
                i += 2
                continue
            if ch == in_quote:
                in_quote = None
            i += 1
            continue

        if ch in ("'", '"'):
            in_quote = ch
            out.append(ch)
            i += 1
            continue

        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                break
            block = source[i : end + 2]
            out.append("\n" if "\n" in block else " ")
            i = end + 2
            continue

        out.append(ch)
        i += 1

    return _cleanup_blank_lines("".join(out))


def _is_ident_char(ch: str) -> bool:
    """Kiểm tra ký tự hợp lệ trong tên định danh JavaScript."""
    return ch.isalnum() or ch in ("_", "$")


def strip_js_comments(source: str) -> str:
    """Loại bỏ an toàn comment `// ...` và `/* ... */` trong JS/ES Module, bảo toàn string, template literal, regex và URL `https://`."""
    out: list[str] = []
    i = 0
    n = len(source)

    # Ngăn xếp ngữ cảnh cho template literal lồng biểu thức `${ ... }`
    # Mỗi phần tử là độ sâu dấu `{` bên trong biểu thức `${ ... }` hiện tại
    template_expr_braces: list[int] = []
    in_template_text = False

    # Theo dõi token ý nghĩa gần nhất để phân biệt `/` (regex literal) với `/` (phép chia)
    last_token_type = "start"  # start | op | keyword | ident | value | close
    last_token_text = ""

    def can_start_regex() -> bool:
        if last_token_type in ("start", "op"):
            return True
        if last_token_type == "keyword" and last_token_text in _JS_REGEX_PRECEDING_KEYWORDS:
            return True
        return False

    while i < n:
        ch = source[i]

        # 1) Đang ở phần văn bản bên trong template literal (`...`)
        if in_template_text:
            if ch == "\\" and i + 1 < n:
                out.append(ch)
                out.append(source[i + 1])
                i += 2
                continue
            if ch == "`":
                out.append(ch)
                in_template_text = False
                last_token_type = "value"
                last_token_text = "`"
                i += 1
                continue
            if ch == "$" and i + 1 < n and source[i + 1] == "{":
                out.append("${")
                in_template_text = False
                template_expr_braces.append(1)
                last_token_type = "op"
                last_token_text = "${"
                i += 2
                continue
            out.append(ch)
            i += 1
            continue

        # 2) Chuỗi dấu nháy đơn hoặc nháy kép
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            while i < n:
                c = source[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(source[i + 1])
                    i += 2
                    continue
                i += 1
                if c == quote:
                    break
            last_token_type = "value"
            last_token_text = quote
            continue

        # 3) Bắt đầu template literal
        if ch == "`":
            out.append(ch)
            in_template_text = True
            i += 1
            continue

        # 4) Comment một dòng `// ...` (giữ lại ký tự xuống dòng để không phá vỡ ASI)
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            i += 2
            while i < n and source[i] not in ("\n", "\r"):
                i += 1
            continue

        # 5) Comment nhiều dòng `/* ... */`
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                break
            block = source[i : end + 2]
            out.append("\n" if "\n" in block else " ")
            i = end + 2
            continue

        # 6) Biểu thức chính quy `/pattern/flags` (chỉ khi đứng sau toán tử/từ khóa hợp lệ)
        if ch == "/" and can_start_regex():
            out.append(ch)
            i += 1
            in_char_class = False
            while i < n:
                c = source[i]
                out.append(c)
                if c == "\\" and i + 1 < n:
                    out.append(source[i + 1])
                    i += 2
                    continue
                if c == "[" and not in_char_class:
                    in_char_class = True
                elif c == "]" and in_char_class:
                    in_char_class = False
                elif c == "/" and not in_char_class:
                    i += 1
                    # Giữ nguyên các cờ regex (g, i, m, u, s, y...)
                    while i < n and source[i].isalpha():
                        out.append(source[i])
                        i += 1
                    break
                elif c in ("\n", "\r"):
                    i += 1
                    break
                i += 1
            last_token_type = "value"
            last_token_text = "/"
            continue

        # 7) Khoảng trắng (không làm đổi `last_token_type`)
        if ch.isspace():
            out.append(ch)
            i += 1
            continue

        # 8) Theo dõi dấu `{` và `}` để quay lại phần văn bản template literal khi kết thúc `${ ... }`
        if ch == "{":
            if template_expr_braces:
                template_expr_braces[-1] += 1
            out.append(ch)
            last_token_type = "op"
            last_token_text = "{"
            i += 1
            continue

        if ch == "}":
            out.append(ch)
            i += 1
            if template_expr_braces:
                template_expr_braces[-1] -= 1
                if template_expr_braces[-1] == 0:
                    template_expr_braces.pop()
                    in_template_text = True
                    continue
            last_token_type = "close"
            last_token_text = "}"
            continue

        # 9) Tên định danh / từ khóa JavaScript
        if _is_ident_char(ch) and not ch.isdigit():
            start = i
            i += 1
            while i < n and _is_ident_char(source[i]):
                i += 1
            word = source[start:i]
            out.append(word)
            if word in _JS_REGEX_PRECEDING_KEYWORDS:
                last_token_type = "keyword"
            else:
                last_token_type = "ident"
            last_token_text = word
            continue

        # 10) Hằng số
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (source[i].isalnum() or source[i] in (".", "_")):
                i += 1
            out.append(source[start:i])
            last_token_type = "value"
            last_token_text = source[start:i]
            continue

        # 11) Dấu ngoặc đóng `)` / `]` (sau đó `/` là phép chia; ngoại lệ `if (...) /re/` hiếm gặp nhưng giữ an toàn)
        if ch in (")", "]"):
            out.append(ch)
            last_token_type = "close"
            last_token_text = ch
            i += 1
            continue

        # 12) Toán tử / dấu phân cách
        if ch in _JS_REGEX_PRECEDING_CHARS or ch == "/":
            out.append(ch)
            last_token_type = "op"
            last_token_text = ch
            i += 1
            continue

        out.append(ch)
        last_token_type = "op"
        last_token_text = ch
        i += 1

    return _cleanup_blank_lines("".join(out))


# Nhận diện lệnh import/export ES module tương đối (`from "./..."` hoặc `from "../..."`) để gắn version cache-bust
_JS_RELATIVE_IMPORT_RE = re.compile(
    r"""(\b(?:from|import)\s*['"])(\.\.?/[^'"?#]+\.m?js)(?:\?[^'"]*)?(['"])"""
)


def rewrite_js_relative_imports(source: str, asset_version: str) -> str:
    """Tự động gắn `?v=<asset_version>` vào các đường dẫn ES module tương đối trong bản JS phục vụ runtime (không sửa tệp trên đĩa)."""
    version = str(asset_version or "").strip()
    if not version:
        return source
    return _JS_RELATIVE_IMPORT_RE.sub(rf"\1\2?v={version}\3", source)


def create_sanitized_static_router(static_dir: Path, *, asset_version: str = "") -> APIRouter:
    """Tạo router phục vụ `/static/{asset_path:path}`: tự động lọc comment JS/CSS & gắn version ES module tại runtime kèm cache theo `mtime_ns`."""
    router = APIRouter(tags=["static"])
    resolved_root = static_dir.resolve()
    # Bộ nhớ đệm nhẹ: path -> (mtime_ns, size, content_bytes, etag, media_type)
    asset_cache: dict[str, tuple[int, int, bytes, str, str]] = {}

    @router.api_route("/static/{asset_path:path}", methods=["GET", "HEAD"])
    async def serve_sanitized_static(asset_path: str, request: Request) -> Response:
        if not asset_path or "\x00" in asset_path:
            raise HTTPException(status_code=404, detail="Không tìm thấy tài nguyên.")

        target = (resolved_root / asset_path).resolve()
        if not target.is_relative_to(resolved_root) or not target.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy tài nguyên.")

        suffix = target.suffix.lower()
        if suffix not in _MEDIA_TYPES:
            return FileResponse(target)

        stat = target.stat()
        cache_key = str(target)
        cached = asset_cache.get(cache_key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            _, _, body_bytes, etag, media_type = cached
        else:
            raw_text = target.read_text(encoding="utf-8")
            if suffix == ".css":
                stripped_text = strip_css_comments(raw_text)
            else:
                stripped_text = rewrite_js_relative_imports(strip_js_comments(raw_text), asset_version)
            body_bytes = stripped_text.encode("utf-8")
            etag = f'"{hashlib.sha256(body_bytes).hexdigest()[:16]}"'
            media_type = _MEDIA_TYPES[suffix]
            asset_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, body_bytes, etag, media_type)

        if_none_match = (request.headers.get("if-none-match") or "").strip()
        if if_none_match and if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})

        return Response(
            content=b"" if request.method == "HEAD" else body_bytes,
            media_type=media_type,
            headers={
                "ETag": etag,
                "Content-Length": str(len(body_bytes)),
            },
        )

    return router
