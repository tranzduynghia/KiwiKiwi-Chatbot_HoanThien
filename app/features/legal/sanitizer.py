"""Bộ lọc làm sạch HTML Rich-Text thuần thư viện chuẩn Python (`html.parser`), không phụ thuộc thư viện ngoài."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote_plus

ALLOWED_TAGS = frozenset({
    "p",
    "br",
    "h1",
    "h2",
    "h3",
    "h4",
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
_DANGEROUS_CSS_RE = re.compile(r"(?:url\s*\(|expression\s*\(|javascript:|vbscript:|data:|@import|<|>|\\|[;{}]|behavior\s*:)", re.IGNORECASE)
_SAFE_FONT_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,60}$")
_FONT_SIZE_RE = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2})?(?:px|rem|em|%))$", re.IGNORECASE)
_TEXT_ALIGN_VALUES = frozenset({"left", "center", "right", "justify"})
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)|rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(?:0|1|0?\.\d+)\s*\)|[a-zA-Z]{3,20})$"
)
_EMPTY_INLINE_RE = re.compile(r"<(strong|b|em|i|u|s|span)(?:\s+[^>]*)?>\s*</\1>", re.IGNORECASE)
_STYLE_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;\"]+)", re.IGNORECASE)

BUILTIN_NON_GOOGLE_FAMILIES = frozenset({
    "inter",
    "jetbrains mono",
    "georgia",
    "system-ui",
    "-apple-system",
    "blinkmacsystemfont",
    "segoe ui",
    "times new roman",
    "times",
    "arial",
    "helvetica",
    "helvetica neue",
    "sans-serif",
    "serif",
    "monospace",
    "ui-sans-serif",
    "ui-serif",
    "ui-monospace",
    "menlo",
    "consolas",
    "courier new",
    "sfmono-regular",
})


def _sanitize_font_family(val: str) -> str:
    """Làm sạch danh sách font-family: chỉ giữ tên phông chữ hợp lệ, chặn CSS injection và ngoặc không cân xứng."""
    cleaned = str(val or "").strip()
    if not cleaned or len(cleaned) > 160 or _DANGEROUS_CSS_RE.search(cleaned):
        return ""
    if cleaned.count("'") % 2 != 0 or cleaned.count('"') % 2 != 0:
        return ""
    safe_parts: list[str] = []
    for part in [p.strip() for p in cleaned.split(",") if p.strip()][:5]:
        if (part.startswith("'") and not part.endswith("'")) or (part.endswith("'") and not part.startswith("'")):
            return ""
        if (part.startswith('"') and not part.endswith('"')) or (part.endswith('"') and not part.startswith('"')):
            return ""
        unquoted = part.strip().strip('"').strip("'").strip()
        if not unquoted or not _SAFE_FONT_TOKEN_RE.fullmatch(unquoted):
            continue
        safe_parts.append(f"'{unquoted}'" if " " in unquoted else unquoted)
    return ", ".join(safe_parts)


def sanitize_inline_style(raw_style: Optional[str]) -> str:
    """Lọc thuộc tính `style` inline: chỉ giữ `font-family`, `font-size` (10px–72px), `text-align`, `color` hợp lệ."""
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
            clean_fam = _sanitize_font_family(val)
            if clean_fam:
                safe_parts.append(f"font-family: {clean_fam}")
        elif prop == "font-size":
            if _FONT_SIZE_RE.fullmatch(val):
                # Hỗ trợ cỡ chữ tùy ý 10px - 72px
                if val.lower().endswith("px"):
                    try:
                        px = float(val[:-2])
                        if 10.0 <= px <= 72.0:
                            fmt = f"{int(px)}px" if px.is_integer() else f"{round(px, 1)}px"
                            safe_parts.append(f"font-size: {fmt}")
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
            escaped_style = (
                style_val.replace("&", "&amp;")
                .replace('"', "&quot;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            safe_attrs.append(f'style="{escaped_style}"')

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
    cleaned = parser.finish()
    for _ in range(4):
        next_cleaned = _EMPTY_INLINE_RE.sub("", cleaned)
        if next_cleaned == cleaned:
            break
        cleaned = next_cleaned
    return cleaned.strip()


def extract_google_font_families(sanitized_html: Optional[str]) -> list[str]:
    """Trích xuất danh sách tên Google Font duy nhất xuất hiện trong HTML đã làm sạch."""
    if not sanitized_html:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for match in _STYLE_FONT_FAMILY_RE.finditer(html.unescape(str(sanitized_html))):
        for part in match.group(1).split(","):
            name = part.strip().strip('"').strip("'").strip()
            if not name or not _SAFE_FONT_TOKEN_RE.fullmatch(name):
                continue
            low = name.lower()
            if low in BUILTIN_NON_GOOGLE_FAMILIES or low in seen:
                continue
            seen.add(low)
            found.append(name)
    return found


def build_google_fonts_stylesheet_urls(sanitized_html: Optional[str]) -> list[str]:
    """Tạo URL `<link>` Google Fonts CSS2 nhẹ (served trực tiếp bởi Google CDN, 0 MB RAM Northflank)."""
    families = extract_google_font_families(sanitized_html)
    if not families:
        return []
    query_parts = [f"family={quote_plus(name)}:wght@400;500;600;700" for name in families[:8]]
    return [f"https://fonts.googleapis.com/css2?{'&'.join(query_parts)}&display=swap"]
