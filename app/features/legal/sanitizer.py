"""Bộ lọc làm sạch HTML Rich-Text thuần thư viện chuẩn Python (`html.parser`), không phụ thuộc thư viện ngoài."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Optional

ALLOWED_TAGS = frozenset({
    "p",
    "br",
    "h1",
    "h2",
    "h3",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "span",
    "div",
    "ul",
    "ol",
    "li",
    "blockquote",
    "hr",
    "a",
})

VOID_TAGS = frozenset({"br", "hr"})

# Các thẻ nguy hiểm bị loại bỏ hoàn toàn cả thẻ lẫn nội dung bên trong
DROP_CONTENT_TAGS = frozenset({
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "applet",
    "svg",
    "math",
    "noscript",
    "template",
})

SAFE_HREF_RE = re.compile(r"^(?:/(?!/)|https://)", re.IGNORECASE)

# Chỉ cho phép 4 thuộc tính CSS inline an toàn
ALLOWED_STYLE_PROPS = frozenset({"font-family", "font-size", "text-align", "color"})
_DANGEROUS_CSS_RE = re.compile(r"(?:url\s*\(|expression\s*\(|javascript:|vbscript:|@import|<|>|behavior\s*:)", re.IGNORECASE)
_FONT_FAMILY_RE = re.compile(r"^[A-Za-z0-9 ,\"'_-]{1,120}$")
_FONT_SIZE_RE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2})?(?:px|rem|em|%))$", re.IGNORECASE)
_TEXT_ALIGN_VALUES = frozenset({"left", "center", "right", "justify"})
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)|rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(?:0|1|0?\.\d+)\s*\)|[a-zA-Z]{3,20})$"
)


def sanitize_inline_style(raw_style: Optional[str]) -> str:
    """Lọc thuộc tính `style` inline: chỉ giữ `font-family`, `font-size`, `text-align`, `color` hợp lệ."""
    if not raw_style:
        return ""
    text = str(raw_style).strip()
    if not text:
        return ""

    safe_parts: list[str] = []
    for declaration in text.split(";"):
        if ":" not in declaration:
            continue
        prop_raw, val_raw = declaration.split(":", 1)
        prop = prop_raw.strip().lower()
        val = " ".join(val_raw.strip().split())
        if prop not in ALLOWED_STYLE_PROPS or not val or _DANGEROUS_CSS_RE.search(val):
            continue

        if prop == "font-family":
            if _FONT_FAMILY_RE.fullmatch(val):
                safe_parts.append(f"font-family: {val}")
        elif prop == "font-size":
            if _FONT_SIZE_RE.fullmatch(val):
                # Giới hạn kích thước chữ hợp lý chống vỡ bố cục (10px - 42px)
                if val.lower().endswith("px"):
                    try:
                        px = float(val[:-2])
                        if 10.0 <= px <= 42.0:
                            safe_parts.append(f"font-size: {val.lower()}")
                    except ValueError:
                        pass
                else:
                    safe_parts.append(f"font-size: {val.lower()}")
        elif prop == "text-align":
            low = val.lower()
            if low in _TEXT_ALIGN_VALUES:
                safe_parts.append(f"text-align: {low}")
        elif prop == "color":
            if _COLOR_RE.fullmatch(val):
                safe_parts.append(f"color: {val}")

    return "; ".join(safe_parts)


def sanitize_href(raw_href: Optional[str]) -> str:
    """Chỉ cho phép đường dẫn nội bộ `/...` hoặc liên kết ngoài `https://...`; chặn tuyệt đối `javascript:` / `data:`."""
    if not raw_href:
        return ""
    cleaned = "".join(ch for ch in str(raw_href).strip() if ord(ch) >= 32 and ord(ch) != 127)
    if not cleaned or not SAFE_HREF_RE.match(cleaned):
        return ""
    return cleaned[:512]


class _AllowlistHTMLSanitizer(HTMLParser):
    """Trình phân tích HTML theo danh sách trắng (allowlist) dùng thuần `html.parser`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open_stack: list[str] = []
        self._drop_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0 or t not in ALLOWED_TAGS:
            return

        attr_map = {k.lower(): (v or "") for k, v in attrs if k and not k.lower().startswith("on")}
        safe_attrs: list[str] = []

        style_val = sanitize_inline_style(attr_map.get("style"))
        if style_val:
            safe_attrs.append(f'style="{html.escape(style_val, quote=True)}"')

        if t == "a":
            href_val = sanitize_href(attr_map.get("href"))
            if href_val:
                safe_attrs.append(f'href="{html.escape(href_val, quote=True)}"')
                if href_val.lower().startswith("https://"):
                    safe_attrs.append('target="_blank"')
                    safe_attrs.append('rel="noopener noreferrer"')

        attr_str = (" " + " ".join(safe_attrs)) if safe_attrs else ""
        if t in VOID_TAGS:
            self._out.append(f"<{t}{attr_str} />")
        else:
            self._out.append(f"<{t}{attr_str}>")
            self._open_stack.append(t)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in DROP_CONTENT_TAGS or self._drop_depth > 0 or t not in ALLOWED_TAGS:
            return
        if t in VOID_TAGS:
            self.handle_starttag(t, attrs)
        else:
            self.handle_starttag(t, attrs)
            self.handle_endtag(t)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in DROP_CONTENT_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if self._drop_depth > 0 or t not in ALLOWED_TAGS or t in VOID_TAGS:
            return
        if t in self._open_stack:
            while self._open_stack:
                top = self._open_stack.pop()
                self._out.append(f"</{top}>")
                if top == t:
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_depth > 0 or not data:
            return
        self._out.append(html.escape(data, quote=False))

    def finish(self) -> str:
        while self._open_stack:
            self._out.append(f"</{self._open_stack.pop()}>")
        return "".join(self._out).strip()


def sanitize_legal_html(raw_html: Optional[str], *, max_length: int = 48000) -> str:
    """Làm sạch nội dung HTML Rich-Text bằng allowlist `html.parser`; nếu là văn bản thuần thì bọc `<p>` an toàn."""
    text = str(raw_html or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    if len(text) > max_length:
        text = text[:max_length]

    # Nếu nội dung cũ là văn bản thuần không có thẻ HTML, tự động chuyển đoạn thành `<p>`
    if "<" not in text:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        return "".join(
            "<p>" + "<br />".join(html.escape(line, quote=False) for line in p.split("\n")) + "</p>"
            for p in paragraphs
        )

    parser = _AllowlistHTMLSanitizer()
    parser.feed(text)
    parser.close()
    return parser.finish()
