/**
 * Bộ điều khiển Tab "Điều khoản & Pháp lý" (5-Page Legal CMS với Rich-Text Editor khởi tạo lười)
 * + Danh sách chấp thuận ToS theo múi giờ IP + Cổng đồng thuận Web toàn màn hình.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges, showConfirmModal } from "../../core/confirmSave.js";
import { closeModalAnimated, openModalAnimated } from "../../core/modalTransition.js";

const TOS_VERSION_RE = /^v\d+\.\d+(?:\.\d+)?$/;
const SAFE_HREF_RE = /^(\/(?!\/)|https:\/\/)/i;
const CUSTOM_FONTS_STORAGE_KEY = "kiwikiwi_custom_google_fonts";
const MIN_FONT_SIZE = 10;
const MAX_FONT_SIZE = 72;
const SAFE_FONT_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,60}$/;

const BUILTIN_FONTS = [
  { id: "inter", label: "Sans-serif (Inter)", family: "'Inter', sans-serif", primary: "Inter" },
  { id: "jetbrains", label: "Monospace (JetBrains Mono)", family: "'JetBrains Mono', monospace", primary: "JetBrains Mono" },
  { id: "georgia", label: "Serif (Georgia)", family: "Georgia, serif", primary: "Georgia" },
  { id: "system", label: "System UI", family: "system-ui, -apple-system, sans-serif", primary: "system-ui" },
];

const NON_GOOGLE_LOWERS = new Set([
  "inter",
  "jetbrains mono",
  "georgia",
  "system-ui",
  "-apple-system",
  "blinkmacsystemfont",
  "segoe ui",
  "sans-serif",
  "serif",
  "monospace",
  "arial",
  "helvetica",
  "times new roman",
]);

const EMPTY_PRUNE_TAGS = new Set(["STRONG", "B", "EM", "I", "U", "S", "SPAN"]);

const ALLOWED_PREVIEW_TAGS = new Set([
  "P",
  "BR",
  "H1",
  "H2",
  "H3",
  "STRONG",
  "B",
  "EM",
  "I",
  "U",
  "S",
  "SPAN",
  "DIV",
  "UL",
  "OL",
  "LI",
  "BLOCKQUOTE",
  "HR",
  "A",
]);
const DROP_PREVIEW_TAGS = new Set(["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "SVG", "MATH"]);

function loadSavedGoogleFonts() {
  try {
    const parsed = JSON.parse(localStorage.getItem(CUSTOM_FONTS_STORAGE_KEY) || "[]");
    if (!Array.isArray(parsed)) return [];
    return parsed.map((n) => String(n || "").trim()).filter((n) => SAFE_FONT_NAME_RE.test(n));
  } catch {
    return [];
  }
}

function persistSavedGoogleFonts(list) {
  try {
    localStorage.setItem(CUSTOM_FONTS_STORAGE_KEY, JSON.stringify(list));
  } catch {
    // Bỏ qua nếu localStorage không khả dụng
  }
}

function parseGoogleFontInput(rawInput) {
  const input = String(rawInput || "").trim();
  if (!input) return { ok: false, error: "Vui lòng nhập tên Google Font hoặc dán URL Google Fonts." };
  let familyName = input;
  if (/^https?:\/\//i.test(input)) {
    try {
      const url = new URL(input);
      if (url.hostname.toLowerCase() !== "fonts.googleapis.com") {
        return { ok: false, error: "Chỉ chấp nhận URL từ https://fonts.googleapis.com/..." };
      }
      const famParam = url.searchParams.get("family") || "";
      if (!famParam) return { ok: false, error: "URL Google Fonts thiếu tham số ?family=..." };
      familyName = decodeURIComponent(famParam.split(":")[0].split("|")[0].replace(/\+/g, " ")).trim();
    } catch {
      return { ok: false, error: "URL Google Fonts không hợp lệ." };
    }
  }
  familyName = familyName.replace(/^['"]+|['"]+$/g, "").replace(/\s+/g, " ").trim();
  if (!SAFE_FONT_NAME_RE.test(familyName)) {
    return { ok: false, error: "Tên phông chữ không hợp lệ (chỉ chữ, số, khoảng trắng, -, _)." };
  }
  return { ok: true, family: familyName };
}

function ensureGoogleFontLoaded(family) {
  const clean = String(family || "").trim();
  if (!clean || !SAFE_FONT_NAME_RE.test(clean)) return;
  const key = clean.toLowerCase();
  if (NON_GOOGLE_LOWERS.has(key)) return;
  if (document.querySelector(`link[data-rt-gfont="${CSS.escape(key)}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  const encoded = encodeURIComponent(clean).replace(/%20/g, "+");
  link.href = `https://fonts.googleapis.com/css2?family=${encoded}:wght@400;500;600;700&display=swap`;
  link.dataset.rtGfont = key;
  document.head.appendChild(link);
}

function loadGoogleFontsUsedInHtml(htmlStr) {
  const re = /font-family\s*:\s*([^;"]+)/gi;
  let match;
  while ((match = re.exec(htmlStr || "")) !== null) {
    match[1].split(",").forEach((part) => {
      const clean = part.trim().replace(/^['"]+|['"]+$/g, "").trim();
      if (clean && SAFE_FONT_NAME_RE.test(clean)) ensureGoogleFontLoaded(clean);
    });
  }
}

function normalizeEditorDom(root) {
  if (!root) return;

  // 1) Đổi <b> -> <strong>, <i> -> <em>
  Array.from(root.querySelectorAll("b, i")).forEach((el) => {
    const rep = document.createElement(el.tagName === "B" ? "strong" : "em");
    while (el.firstChild) rep.appendChild(el.firstChild);
    if (el.getAttribute("style")) rep.setAttribute("style", el.getAttribute("style"));
    el.replaceWith(rep);
  });

  // 2) Đưa khoảng trắng thừa ở biên trong thẻ định dạng ra ngoài (tránh lỗi **text **)
  Array.from(root.querySelectorAll("strong, em, u, s")).forEach((el) => {
    if (!el.parentNode) return;
    const first = el.firstChild;
    if (first && first.nodeType === Node.TEXT_NODE) {
      const m = /^([ \u00A0]+)/.exec(first.nodeValue || "");
      if (m) {
        first.nodeValue = first.nodeValue.slice(m[1].length);
        el.parentNode.insertBefore(document.createTextNode(m[1]), el);
      }
    }
    const last = el.lastChild;
    if (last && last.nodeType === Node.TEXT_NODE) {
      const m = /([ \u00A0]+)$/.exec(last.nodeValue || "");
      if (m) {
        last.nodeValue = last.nodeValue.slice(0, -m[1].length);
        el.parentNode.insertBefore(document.createTextNode(m[1]), el.nextSibling);
      }
    }
  });

  // 3) Dọn thẻ inline rỗng, gỡ <span> không có style, gộp thẻ liền kề giống hệt nhau
  for (let pass = 0; pass < 4; pass++) {
    let changed = false;
    Array.from(root.querySelectorAll("strong, b, em, i, u, s, span")).forEach((el) => {
      if (!el.parentNode) return;
      if (el.tagName === "SPAN" && !(el.getAttribute("style") || "").trim()) {
        while (el.firstChild) el.parentNode.insertBefore(el.firstChild, el);
        el.remove();
        changed = true;
        return;
      }
      if (EMPTY_PRUNE_TAGS.has(el.tagName) && !el.querySelector("br") && (el.textContent || "").length === 0) {
        el.remove();
        changed = true;
      }
    });

    Array.from(root.querySelectorAll("strong, em, u, s, span")).forEach((el) => {
      if (!el.parentNode) return;
      const next = el.nextSibling;
      if (
        next &&
        next.nodeType === Node.ELEMENT_NODE &&
        next.tagName === el.tagName &&
        (next.getAttribute("style") || "") === (el.getAttribute("style") || "")
      ) {
        while (next.firstChild) el.appendChild(next.firstChild);
        next.remove();
        changed = true;
      }
    });
    if (!changed) break;
  }

  root.normalize();
}

function isElementBold(el) {
  if (!el || el.nodeType !== Node.ELEMENT_NODE) return false;
  if (el.tagName === "STRONG" || el.tagName === "B") return true;
  const inlineW = (el.style && el.style.fontWeight) || "";
  if (inlineW === "bold" || inlineW === "bolder" || parseInt(inlineW, 10) >= 600) return true;
  const comp = window.getComputedStyle(el).fontWeight;
  return comp === "bold" || comp === "bolder" || parseInt(comp, 10) >= 600;
}

function isNodeInsideBold(node, root) {
  let cur = node.nodeType === Node.TEXT_NODE ? node.parentElement : node;
  while (cur && cur !== root) {
    if (isElementBold(cur)) return true;
    cur = cur.parentElement;
  }
  return false;
}

function stripBoldFromFragment(fragment) {
  Array.from(fragment.querySelectorAll("strong, b")).forEach((el) => {
    if (!el.parentNode) return;
    while (el.firstChild) el.parentNode.insertBefore(el.firstChild, el);
    el.remove();
  });
  Array.from(fragment.querySelectorAll("[style]")).forEach((el) => {
    if (el.style && el.style.fontWeight) {
      el.style.removeProperty("font-weight");
      if (!el.getAttribute("style")?.trim()) el.removeAttribute("style");
    }
  });
}

function collectTextNodesInRange(range, root) {
  const nodes = [];
  if (range.collapsed) {
    if (root.contains(range.startContainer)) nodes.push(range.startContainer);
    return nodes;
  }
  const common = range.commonAncestorContainer;
  const walkerRoot = common.nodeType === Node.TEXT_NODE ? common.parentNode : common;
  if (!walkerRoot || !root.contains(walkerRoot)) return nodes;
  const walker = document.createTreeWalker(walkerRoot, NodeFilter.SHOW_TEXT);
  let cur;
  while ((cur = walker.nextNode())) {
    if (!cur.nodeValue || !cur.nodeValue.length) continue;
    try {
      if (range.intersectsNode(cur)) nodes.push(cur);
    } catch {
      // Bỏ qua node không thuộc DOM
    }
  }
  return nodes;
}

function splitBoldAncestorsAtRange(range, editorEl) {
  let cur = range.commonAncestorContainer;
  if (cur.nodeType === Node.TEXT_NODE) cur = cur.parentElement;
  const boldAncestors = [];
  while (cur && cur !== editorEl) {
    if (
      cur.tagName === "STRONG" ||
      cur.tagName === "B" ||
      (cur.style && (cur.style.fontWeight === "bold" || parseInt(cur.style.fontWeight, 10) >= 600))
    ) {
      boldAncestors.push(cur);
    }
    cur = cur.parentElement;
  }
  if (!boldAncestors.length) return;
  const outermost = boldAncestors[boldAncestors.length - 1];
  const beforeRange = document.createRange();
  beforeRange.selectNodeContents(outermost);
  beforeRange.setEnd(range.startContainer, range.startOffset);

  const afterRange = document.createRange();
  afterRange.selectNodeContents(outermost);
  afterRange.setStart(range.endContainer, range.endOffset);

  const beforeFrag = beforeRange.extractContents();
  const afterFrag = afterRange.extractContents();
  const middleFrag = range.extractContents();
  stripBoldFromFragment(middleFrag);

  const parent = outermost.parentNode;
  if (!parent) return;

  if ((beforeFrag.textContent || "").length > 0 || beforeFrag.querySelector("br")) {
    const beforeClone = outermost.cloneNode(false);
    beforeClone.appendChild(beforeFrag);
    parent.insertBefore(beforeClone, outermost);
  }
  const mStart = document.createTextNode("");
  const mEnd = document.createTextNode("");
  parent.insertBefore(mStart, outermost);
  parent.insertBefore(middleFrag, outermost);
  parent.insertBefore(mEnd, outermost);
  if ((afterFrag.textContent || "").length > 0 || afterFrag.querySelector("br")) {
    const afterClone = outermost.cloneNode(false);
    afterClone.appendChild(afterFrag);
    parent.insertBefore(afterClone, outermost);
  }
  outermost.remove();

  const nextRange = document.createRange();
  nextRange.setStartAfter(mStart);
  nextRange.setEndBefore(mEnd);
  const sel = window.getSelection();
  if (sel) {
    sel.removeAllRanges();
    sel.addRange(nextRange);
  }
  mStart.remove();
  mEnd.remove();
}

function detectBrowserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

/**
 * Lọc an toàn HTML ở phía client trước khi đổ vào khung Xem trước (Live Preview)
 * đồng bộ quy tắc allowlist với bộ lọc `html.parser` trên server.
 */
function sanitizePreviewHtml(rawHtml) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(String(rawHtml || ""), "text/html");

  function cleanNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      return document.createTextNode(node.textContent || "");
    }
    if (node.nodeType !== Node.ELEMENT_NODE) {
      return document.createDocumentFragment();
    }
    const tag = node.tagName.toUpperCase();
    if (DROP_PREVIEW_TAGS.has(tag)) {
      return document.createDocumentFragment();
    }
    if (!ALLOWED_PREVIEW_TAGS.has(tag)) {
      const frag = document.createDocumentFragment();
      Array.from(node.childNodes).forEach((child) => frag.appendChild(cleanNode(child)));
      return frag;
    }

    const el = document.createElement(tag.toLowerCase());
    const allowedStyles = [];
    if (node.style) {
      if (node.style.fontFamily) allowedStyles.push(`font-family: ${node.style.fontFamily}`);
      if (node.style.fontSize) allowedStyles.push(`font-size: ${node.style.fontSize}`);
      if (node.style.textAlign) allowedStyles.push(`text-align: ${node.style.textAlign}`);
      if (node.style.color) allowedStyles.push(`color: ${node.style.color}`);
    }
    if (allowedStyles.length) {
      el.setAttribute("style", allowedStyles.join("; "));
    }

    if (tag === "A") {
      const href = (node.getAttribute("href") || "").trim();
      if (SAFE_HREF_RE.test(href)) {
        el.setAttribute("href", href);
        if (/^https:\/\//i.test(href)) {
          el.setAttribute("target", "_blank");
          el.setAttribute("rel", "noopener noreferrer");
        }
      }
    }

    Array.from(node.childNodes).forEach((child) => el.appendChild(cleanNode(child)));
    return el;
  }

  const wrapper = document.createElement("div");
  Array.from(doc.body.childNodes).forEach((child) => wrapper.appendChild(cleanNode(child)));
  return wrapper.innerHTML;
}

export function createLegalCmsTabController({ isAdmin } = {}) {
  let editorInitialized = false;
  let currentDocKey = "toslinks";
  let activeTosVersion = "v1.0";
  let initialSnapshot = null;
  let savedSelectionRange = null;
  let viewMode = "edit";
  let savedGoogleFonts = loadSavedGoogleFonts();
  let toolbarRafId = null;
  let isFontPopoverOpen = false;

  let currentCursor = "0";
  let nextCursor = null;
  let prevCursor = null;
  let currentSearch = "";
  let discordPromptBaseUrl = "https://kiwikiwi.dpdns.org";
  let discordPreviewMode = "welcome";
  let initialDiscordPromptSnapshot = null;

  function renderDiscordPromptPreview() {
    const welcomeEl = document.getElementById("legal-discord-welcome-tpl");
    const updateEl = document.getElementById("legal-discord-update-tpl");
    const btnLabelEl = document.getElementById("legal-discord-btn-label");
    const previewTextEl = document.getElementById("legal-discord-preview-text");
    const previewBtnEl = document.getElementById("legal-discord-preview-btn");
    const modeBadgeEl = document.getElementById("legal-discord-preview-mode-badge");
    const welcomeTab = document.getElementById("btn-discord-preview-welcome");
    const updateTab = document.getElementById("btn-discord-preview-update");

    if (welcomeTab) welcomeTab.setAttribute("aria-pressed", String(discordPreviewMode === "welcome"));
    if (updateTab) updateTab.setAttribute("aria-pressed", String(discordPreviewMode === "update"));
    if (modeBadgeEl) {
      modeBadgeEl.textContent = discordPreviewMode === "update" ? "Cập nhật phiên bản" : "Lần đầu";
    }

    const verVal = (document.getElementById("legal-tos-version")?.value || "").trim() || activeTosVersion || "v1.0";
    const rawTpl =
      discordPreviewMode === "update"
        ? (updateEl?.value || "").trim()
        : (welcomeEl?.value || "").trim();
    const rendered = rawTpl
      .replace(/\{version\}/g, verVal)
      .replace(/\{previous_version\}/g, "v1.0")
      .replace(/\{base_url\}/g, discordPromptBaseUrl);

    if (previewTextEl) previewTextEl.textContent = rendered || "(Chưa nhập mẫu tin nhắn)";
    const btnLabel = (btnLabelEl?.value || "").trim() || "Tôi đồng ý (Accept Terms)";
    if (previewBtnEl) previewBtnEl.textContent = `✅ ${btnLabel}`;
  }

  function getEditorEl() {
    return document.getElementById("legal-rte-editor");
  }

  function saveSelection() {
    const editor = getEditorEl();
    const sel = window.getSelection();
    if (!editor || !sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (editor.contains(range.commonAncestorContainer)) {
      savedSelectionRange = range.cloneRange();
    }
  }

  function restoreSelection() {
    const editor = getEditorEl();
    if (!editor) return;
    editor.focus();
    if (savedSelectionRange) {
      const sel = window.getSelection();
      if (sel) {
        sel.removeAllRanges();
        sel.addRange(savedSelectionRange);
      }
    }
  }

  function applyInlineSpanStyle(prop, value) {
    const editor = getEditorEl();
    if (!editor) return;
    restoreSelection();
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;

    const cssProp = prop === "fontSize" ? "font-size" : prop === "fontFamily" ? "font-family" : prop;
    const cleanVal = String(value || "").trim();

    if (range.collapsed) {
      const span = document.createElement("span");
      if (cleanVal) span.style.setProperty(cssProp, cleanVal);
      span.appendChild(document.createTextNode("\u200B"));
      range.insertNode(span);
      const nextRange = document.createRange();
      nextRange.setStart(span.firstChild, 1);
      nextRange.collapse(true);
      sel.removeAllRanges();
      sel.addRange(nextRange);
    } else {
      const frag = range.extractContents();
      Array.from(frag.querySelectorAll("[style]")).forEach((el) => {
        el.style.removeProperty(cssProp);
        if (!el.getAttribute("style")?.trim()) el.removeAttribute("style");
      });
      const span = document.createElement("span");
      if (cleanVal) span.style.setProperty(cssProp, cleanVal);
      span.appendChild(frag);
      range.insertNode(span);
      const nextRange = document.createRange();
      nextRange.selectNodeContents(span);
      sel.removeAllRanges();
      sel.addRange(nextRange);
    }
    normalizeEditorDom(editor);
    saveSelection();
    updateCharCount();
    scheduleToolbarSync();
  }

  function applyBoldToggle() {
    const editor = getEditorEl();
    if (!editor) return;
    restoreSelection();
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;

    if (range.collapsed) {
      document.execCommand("bold", false, null);
    } else {
      const textNodes = collectTextNodesInRange(range, editor);
      const allBold = textNodes.length > 0 && textNodes.every((n) => isNodeInsideBold(n, editor));
      if (allBold) {
        splitBoldAncestorsAtRange(range, editor);
        const curSel = window.getSelection();
        if (curSel && curSel.rangeCount > 0) {
          const curRange = curSel.getRangeAt(0);
          const frag = curRange.extractContents();
          stripBoldFromFragment(frag);
          const sMark = document.createTextNode("");
          const eMark = document.createTextNode("");
          curRange.insertNode(eMark);
          curRange.insertNode(frag);
          curRange.insertNode(sMark);
          const finalRange = document.createRange();
          finalRange.setStartAfter(sMark);
          finalRange.setEndBefore(eMark);
          curSel.removeAllRanges();
          curSel.addRange(finalRange);
          sMark.remove();
          eMark.remove();
        }
      } else {
        const frag = range.extractContents();
        stripBoldFromFragment(frag);
        const strong = document.createElement("strong");
        strong.appendChild(frag);
        range.insertNode(strong);
        const nextRange = document.createRange();
        nextRange.selectNodeContents(strong);
        sel.removeAllRanges();
        sel.addRange(nextRange);
      }
    }
    normalizeEditorDom(editor);
    saveSelection();
    updateCharCount();
    scheduleToolbarSync();
  }

  function updateToolbarFromSelection() {
    toolbarRafId = null;
    const editor = getEditorEl();
    const sel = window.getSelection();
    const sizeInput = document.getElementById("rte-font-size");
    if (!editor || !sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (!editor.contains(range.commonAncestorContainer)) return;

    saveSelection();
    const textNodes = collectTextNodesInRange(range, editor);
    const elements = textNodes.length
      ? textNodes.map((n) => (n.nodeType === Node.TEXT_NODE ? n.parentElement : n)).filter(Boolean)
      : [
          range.startContainer.nodeType === Node.TEXT_NODE
            ? range.startContainer.parentElement
            : range.startContainer,
        ].filter(Boolean);

    if (sizeInput && document.activeElement !== sizeInput && elements.length) {
      const sizes = new Set();
      elements.forEach((el) => {
        const px = parseFloat(window.getComputedStyle(el).fontSize || "14");
        if (!Number.isNaN(px)) sizes.add(Math.round(px));
      });
      if (sizes.size === 1) {
        sizeInput.value = String(Array.from(sizes)[0]);
        sizeInput.placeholder = "14";
      } else if (sizes.size > 1) {
        sizeInput.value = "";
        sizeInput.placeholder = "Hỗn hợp";
      }
    }

    const triggerLabel = document.getElementById("rte-font-trigger-label");
    if (triggerLabel && elements.length) {
      const fams = new Set();
      elements.forEach((el) => {
        const first = (window.getComputedStyle(el).fontFamily || "").split(",")[0].trim().replace(/^['"]+|['"]+$/g, "");
        if (first) fams.add(first);
      });
      if (fams.size === 1) {
        const onlyFam = Array.from(fams)[0];
        const builtin = BUILTIN_FONTS.find((f) => f.primary.toLowerCase() === onlyFam.toLowerCase());
        triggerLabel.textContent = builtin ? builtin.label : onlyFam;
      } else if (fams.size > 1) {
        triggerLabel.textContent = "Hỗn hợp";
      }
    }

    const boldBtn = document.querySelector('[data-rte-cmd="bold"]');
    if (boldBtn) {
      const isBoldActive = elements.length > 0 && elements.every((el) => isNodeInsideBold(el, editor));
      boldBtn.setAttribute("aria-pressed", String(isBoldActive));
    }
  }

  function scheduleToolbarSync() {
    if (toolbarRafId !== null) return;
    toolbarRafId = requestAnimationFrame(updateToolbarFromSelection);
  }

  function applyCustomFontSize(rawValue) {
    const trimmed = String(rawValue ?? "").trim();
    const sizeInput = document.getElementById("rte-font-size");
    if (!trimmed) {
      applyInlineSpanStyle("fontSize", "");
      return;
    }
    const parsed = parseFloat(trimmed);
    if (Number.isNaN(parsed)) return;
    const clamped = Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, Math.round(parsed)));
    if (sizeInput) {
      sizeInput.value = String(clamped);
      sizeInput.placeholder = "14";
    }
    applyInlineSpanStyle("fontSize", `${clamped}px`);
  }

  function renderFontPopover() {
    const builtinList = document.getElementById("rte-builtin-fonts-list");
    const customSection = document.getElementById("rte-custom-fonts-section");
    const customList = document.getElementById("rte-custom-fonts-list");

    if (builtinList) {
      builtinList.replaceChildren();
      BUILTIN_FONTS.forEach((font) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "rt-font-option";
        btn.setAttribute("role", "option");
        btn.style.fontFamily = font.family;
        btn.textContent = font.label;
        btn.addEventListener("mousedown", (e) => e.preventDefault());
        btn.addEventListener("click", () => {
          applyInlineSpanStyle("fontFamily", font.family);
          const lbl = document.getElementById("rte-font-trigger-label");
          if (lbl) lbl.textContent = font.label;
          closeFontPopover();
        });
        builtinList.appendChild(btn);
      });
    }

    if (customSection && customList) {
      customList.replaceChildren();
      if (!savedGoogleFonts.length) {
        customSection.classList.add("hidden");
      } else {
        customSection.classList.remove("hidden");
        savedGoogleFonts.forEach((family) => {
          ensureGoogleFontLoaded(family);
          const row = document.createElement("div");
          row.className = "flex items-center justify-between gap-1";

          const pickBtn = document.createElement("button");
          pickBtn.type = "button";
          pickBtn.className = "rt-font-option flex-1 text-left truncate";
          pickBtn.setAttribute("role", "option");
          const cssFam = family.includes(" ") ? `'${family}', sans-serif` : `${family}, sans-serif`;
          pickBtn.style.fontFamily = cssFam;
          pickBtn.textContent = family;
          pickBtn.addEventListener("mousedown", (e) => e.preventDefault());
          pickBtn.addEventListener("click", () => {
            ensureGoogleFontLoaded(family);
            applyInlineSpanStyle("fontFamily", cssFam);
            const lbl = document.getElementById("rte-font-trigger-label");
            if (lbl) lbl.textContent = family;
            closeFontPopover();
          });

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "rt-font-delete-btn shrink-0";
          delBtn.title = `Xóa phông ${family}`;
          delBtn.textContent = "✕";
          delBtn.addEventListener("mousedown", (e) => e.preventDefault());
          delBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            savedGoogleFonts = savedGoogleFonts.filter((item) => item.toLowerCase() !== family.toLowerCase());
            persistSavedGoogleFonts(savedGoogleFonts);
            renderFontPopover();
            showToast(`Đã xóa phông "${family}".`);
          });

          row.appendChild(pickBtn);
          row.appendChild(delBtn);
          customList.appendChild(row);
        });
      }
    }
  }

  function openFontPopover() {
    const popover = document.getElementById("rte-font-popover");
    const trigger = document.getElementById("rte-font-family");
    if (!popover || !trigger) return;
    renderFontPopover();
    isFontPopoverOpen = true;
    popover.classList.remove("hidden");
    requestAnimationFrame(() => popover.classList.add("is-open"));
    trigger.setAttribute("aria-expanded", "true");
  }

  function closeFontPopover() {
    const popover = document.getElementById("rte-font-popover");
    const trigger = document.getElementById("rte-font-family");
    if (!popover || !trigger) return;
    isFontPopoverOpen = false;
    popover.classList.remove("is-open");
    popover.classList.add("hidden");
    trigger.setAttribute("aria-expanded", "false");
  }

  function openGoogleFontModal() {
    closeFontPopover();
    const modal = document.getElementById("google-font-modal");
    const input = document.getElementById("gf-input");
    const errEl = document.getElementById("gf-error");
    const previewName = document.getElementById("gf-preview-name");
    const previewSample = document.getElementById("gf-preview-sample");
    if (!modal) return;
    if (input) input.value = "";
    if (errEl) {
      errEl.textContent = "";
      errEl.classList.add("hidden");
    }
    if (previewName) previewName.textContent = "—";
    if (previewSample) previewSample.style.fontFamily = "";
    modal.classList.remove("hidden");
    modal.classList.add("flex");
    requestAnimationFrame(() => {
      modal.classList.add("is-open");
      if (input) input.focus();
    });
  }

  function closeGoogleFontModal() {
    const modal = document.getElementById("google-font-modal");
    if (!modal) return;
    modal.classList.remove("is-open");
    modal.classList.add("hidden");
    modal.classList.remove("flex");
  }

  function previewGoogleFontInput() {
    const input = document.getElementById("gf-input");
    const errEl = document.getElementById("gf-error");
    const previewName = document.getElementById("gf-preview-name");
    const previewSample = document.getElementById("gf-preview-sample");
    const res = parseGoogleFontInput(input?.value || "");
    if (!res.ok) {
      if (errEl) {
        errEl.textContent = res.error;
        errEl.classList.remove("hidden");
      }
      return null;
    }
    if (errEl) {
      errEl.textContent = "";
      errEl.classList.add("hidden");
    }
    ensureGoogleFontLoaded(res.family);
    const cssFam = res.family.includes(" ") ? `'${res.family}', sans-serif` : `${res.family}, sans-serif`;
    if (previewName) previewName.textContent = res.family;
    if (previewSample) previewSample.style.fontFamily = cssFam;
    return res.family;
  }

  function updateCharCount() {
    const editor = getEditorEl();
    const charEl = document.getElementById("legal-body-chars");
    if (editor && charEl) {
      charEl.textContent = `${editor.innerHTML.length.toLocaleString("vi-VN")} ký tự HTML`;
    }
  }

  function setViewMode(mode) {
    viewMode = mode === "preview" ? "preview" : "edit";
    const rteWrap = document.getElementById("legal-rte-container");
    const previewWrap = document.getElementById("legal-rte-preview-wrap");
    const editBtn = document.getElementById("btn-rte-mode-edit");
    const prevBtn = document.getElementById("btn-rte-mode-preview");

    if (editBtn) editBtn.setAttribute("aria-pressed", String(viewMode === "edit"));
    if (prevBtn) prevBtn.setAttribute("aria-pressed", String(viewMode === "preview"));

    if (viewMode === "preview") {
      const titleVal = (document.getElementById("legal-doc-title")?.value || "").trim();
      const subVal = (document.getElementById("legal-doc-subtitle")?.value || "").trim();
      const verVal = (document.getElementById("legal-tos-version")?.value || "").trim() || activeTosVersion;
      const editor = getEditorEl();

      const pTitle = document.getElementById("legal-preview-title");
      const pSub = document.getElementById("legal-preview-subtitle");
      const pVer = document.getElementById("legal-preview-version");
      const pBody = document.getElementById("legal-preview-body");
      if (pTitle) pTitle.textContent = titleVal || "(Chưa có tiêu đề)";
      if (pSub) pSub.textContent = subVal;
      if (pVer) pVer.textContent = verVal;
      if (pBody && editor) {
        normalizeEditorDom(editor);
        const safePreview = sanitizePreviewHtml(editor.innerHTML);
        loadGoogleFontsUsedInHtml(safePreview);
        pBody.innerHTML = safePreview;
      }

      if (rteWrap) rteWrap.classList.add("hidden");
      if (previewWrap) previewWrap.classList.remove("hidden");
    } else {
      if (previewWrap) previewWrap.classList.add("hidden");
      if (rteWrap) rteWrap.classList.remove("hidden");
    }
  }

  function syncPageTabs() {
    document.querySelectorAll(".ox-legal-page-tab").forEach((btn) => {
      btn.dataset.active = String(btn.dataset.legalPage === currentDocKey);
    });
    const routeBadge = document.getElementById("legal-current-route-badge");
    if (routeBadge) routeBadge.textContent = `/${currentDocKey}`;
  }

  async function loadPage(docKey = currentDocKey) {
    currentDocKey = docKey;
    syncPageTabs();
    try {
      const tz = detectBrowserTimezone();
      const params = new URLSearchParams({ doc_key: currentDocKey });
      if (tz) params.set("tz", tz);
      const data = await apiRequest(`/api/legal?${params.toString()}`, { cache: "no-store" });

      activeTosVersion = data.active_tos_version || "v1.0";
      const doc = data.doc || { title: "", subtitle: "", body: "" };

      const verBadge = document.getElementById("legal-active-version-badge");
      if (verBadge) verBadge.textContent = `Phiên bản: ${activeTosVersion}`;
      const updBadge = document.getElementById("legal-updated-badge");
      if (updBadge) updBadge.textContent = `Cập nhật: ${doc.tos_updated_at_formatted || data.tos_updated_at_formatted || "—"}`;
      const tzBadge = document.getElementById("legal-timezone-badge");
      if (tzBadge && data.timezone_label) {
        tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      }

      const titleEl = document.getElementById("legal-doc-title");
      const subEl = document.getElementById("legal-doc-subtitle");
      const verEl = document.getElementById("legal-tos-version");
      const editor = getEditorEl();

      if (titleEl) titleEl.value = doc.title || "";
      if (subEl) subEl.value = doc.subtitle || "";
      if (verEl) verEl.value = activeTosVersion;
      if (editor) {
        const safeLoaded = sanitizePreviewHtml(doc.body || "");
        loadGoogleFontsUsedInHtml(safeLoaded);
        editor.innerHTML = safeLoaded;
        normalizeEditorDom(editor);
      }

      // Nạp cấu hình mẫu tin nhắn yêu cầu đồng ý ToS trên Discord
      const dp = data.discord_prompt || {};
      discordPromptBaseUrl = dp.base_url || "https://kiwikiwi.dpdns.org";
      const welcomeEl = document.getElementById("legal-discord-welcome-tpl");
      const updateEl = document.getElementById("legal-discord-update-tpl");
      const btnLabelEl = document.getElementById("legal-discord-btn-label");
      if (welcomeEl && dp.discord_tos_welcome_template !== undefined) {
        welcomeEl.value = dp.discord_tos_welcome_template || "";
      }
      if (updateEl && dp.discord_tos_update_template !== undefined) {
        updateEl.value = dp.discord_tos_update_template || "";
      }
      if (btnLabelEl && dp.discord_tos_button_label !== undefined) {
        btnLabelEl.value = dp.discord_tos_button_label || "";
      }
      initialDiscordPromptSnapshot = {
        "legal.discord_tos_welcome_template": welcomeEl ? welcomeEl.value : "",
        "legal.discord_tos_update_template": updateEl ? updateEl.value : "",
        "legal.discord_tos_button_label": btnLabelEl ? btnLabelEl.value : "",
      };
      renderDiscordPromptPreview();

      initialSnapshot = {
        "legal.active_tos_version": activeTosVersion,
        [`legal.docs.${currentDocKey}.title`]: doc.title || "",
        [`legal.docs.${currentDocKey}.subtitle`]: doc.subtitle || "",
        [`legal.docs.${currentDocKey}.body`]: editor ? editor.innerHTML : doc.body || "",
      };
      updateCharCount();
      if (viewMode === "preview") setViewMode("preview");
    } catch (err) {
      showToast("Không tải được tài liệu Pháp lý: " + err.message, false);
    }
  }

  async function loadAcceptances(cursor = "0") {
    const tbody = document.getElementById("tos-accept-tbody");
    try {
      const tz = detectBrowserTimezone();
      const params = new URLSearchParams({
        limit: "15",
        cursor: String(cursor || "0"),
      });
      if (currentSearch) params.set("q", currentSearch);
      if (tz) params.set("tz", tz);

      const data = await apiRequest(`/api/legal/acceptances?${params.toString()}`);
      currentCursor = data.cursor || "0";
      nextCursor = data.next_cursor;
      prevCursor = data.prev_cursor;

      const tzBadge = document.getElementById("legal-timezone-badge");
      if (tzBadge && data.timezone_label) {
        tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      }
      const countEl = document.getElementById("tos-accept-count");
      if (countEl) countEl.textContent = `${data.total || 0} lượt chấp thuận`;

      const pageNum = Math.floor(Number(currentCursor || 0) / (data.limit || 15)) + 1;
      const pageInfo = document.getElementById("tos-accept-page-info");
      if (pageInfo) pageInfo.textContent = `Trang ${pageNum} · Hiển thị ${data.items.length}/${data.total || 0}`;

      const prevBtn = document.getElementById("btn-tos-prev");
      const nextBtn = document.getElementById("btn-tos-next");
      if (prevBtn) prevBtn.disabled = prevCursor === null || prevCursor === undefined;
      if (nextBtn) nextBtn.disabled = !data.has_more;

      if (!tbody) return;
      if (!data.items.length) {
        tbody.innerHTML =
          '<tr><td colspan="6" class="py-6 text-center ox-text-secondary">Chưa có người dùng nào ghi nhận chấp thuận ToS.</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const verBadgeClass = item.is_current_version
            ? "ox-badge-emerald"
            : "ox-badge-amber";
          const verNote = item.is_current_version ? "Hiện hành" : `Cũ (cần ${escapeHtml(data.active_tos_version)})`;
          const sourceBadge =
            item.consent_source === "web"
              ? '<span class="px-2 py-0.5 rounded-full text-[10px] bg-sky-500/15 text-sky-300 border border-sky-500/30">Web</span>'
              : '<span class="px-2 py-0.5 rounded-full text-[10px] bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">Discord</span>';
          const revokeBtn = isAdmin
            ? `<button type="button" data-revoke-tos-uid="${escapeHtml(item.discord_user_id)}" data-revoke-tos-ver="${escapeHtml(item.tos_accepted_version)}" data-revoke-tos-name="${escapeHtml(item.discord_username || item.discord_user_id)}" class="btn-danger text-[11px] px-2 py-1 whitespace-nowrap">Thu hồi</button>`
            : "—";
          return `
            <tr class="ox-table-row">
              <td class="py-2.5 pr-3 font-mono ox-text-primary">${escapeHtml(item.discord_user_id)}</td>
              <td class="py-2.5 pr-3 ox-text-primary font-medium">${escapeHtml(item.discord_username || "—")}</td>
              <td class="py-2.5 pr-3">
                <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border font-mono text-[10px] ${verBadgeClass}">
                  ${escapeHtml(item.tos_accepted_version)} · ${verNote}
                </span>
              </td>
              <td class="py-2.5 pr-3">${sourceBadge}</td>
              <td class="py-2.5 pr-3 text-right font-mono ox-text-secondary">${escapeHtml(item.tos_accepted_at_formatted || "—")}</td>
              <td class="py-2.5 text-right whitespace-nowrap">${revokeBtn}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="6" class="py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
      }
    }
  }

  /**
   * Khởi tạo lười toàn bộ trình lắng nghe Rich-Text Editor chỉ khi Admin mở tab "Điều khoản & Pháp lý".
   */
  function ensureEditorInitialized() {
    if (editorInitialized) return;
    editorInitialized = true;

    const tabsWrap = document.getElementById("legal-page-tabs");
    if (tabsWrap) {
      tabsWrap.addEventListener("click", (event) => {
        const btn = event.target.closest("button[data-legal-page]");
        if (!btn) return;
        loadPage(btn.dataset.legalPage);
      });
    }

    const editModeBtn = document.getElementById("btn-rte-mode-edit");
    const previewModeBtn = document.getElementById("btn-rte-mode-preview");
    if (editModeBtn) editModeBtn.addEventListener("click", () => setViewMode("edit"));
    if (previewModeBtn) previewModeBtn.addEventListener("click", () => setViewMode("preview"));

    const editor = getEditorEl();
    if (editor) {
      editor.addEventListener("keyup", () => {
        scheduleToolbarSync();
        updateCharCount();
      });
      editor.addEventListener("mouseup", scheduleToolbarSync);
      editor.addEventListener("input", () => {
        updateCharCount();
        scheduleToolbarSync();
      });
      editor.addEventListener("keydown", (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "b") {
          event.preventDefault();
          applyBoldToggle();
        }
      });
    }

    document.addEventListener("selectionchange", () => {
      const ed = getEditorEl();
      const sel = window.getSelection();
      if (!ed || !sel || sel.rangeCount === 0) return;
      if (ed.contains(sel.getRangeAt(0).commonAncestorContainer)) {
        scheduleToolbarSync();
      }
    });

    const toolbar = document.getElementById("legal-rte-toolbar");
    if (toolbar && isAdmin) {
      toolbar.addEventListener("mousedown", (event) => {
        if (event.target.closest("input") || event.target.closest("select")) return;
        if (event.target.closest("button")) {
          // Giữ nguyên vùng chọn chữ trong contenteditable khi nhấn nút toolbar
          event.preventDefault();
          saveSelection();
        }
      });

      toolbar.addEventListener("click", (event) => {
        const cmdBtn = event.target.closest("button[data-rte-cmd]");
        if (cmdBtn) {
          if (cmdBtn.dataset.rteCmd === "bold") {
            applyBoldToggle();
            return;
          }
          restoreSelection();
          document.execCommand(cmdBtn.dataset.rteCmd, false, null);
          if (editor) normalizeEditorDom(editor);
          saveSelection();
          updateCharCount();
          scheduleToolbarSync();
          return;
        }
        const actionBtn = event.target.closest("button[data-rte-action]");
        if (actionBtn && actionBtn.dataset.rteAction === "blockquote") {
          restoreSelection();
          document.execCommand("formatBlock", false, "blockquote");
          if (editor) normalizeEditorDom(editor);
          saveSelection();
          updateCharCount();
          scheduleToolbarSync();
        }
      });

      // Custom Font Popover & Google Fonts Modal
      const fontTrigger = document.getElementById("rte-font-family");
      if (fontTrigger) {
        fontTrigger.addEventListener("click", (e) => {
          e.stopPropagation();
          if (isFontPopoverOpen) closeFontPopover();
          else openFontPopover();
        });
      }
      document.addEventListener("click", (e) => {
        if (isFontPopoverOpen && !e.target.closest("#rte-font-picker")) closeFontPopover();
      });

      const openGfBtn = document.getElementById("btn-open-google-font-modal");
      if (openGfBtn) openGfBtn.addEventListener("click", openGoogleFontModal);
      const gfForm = document.getElementById("google-font-form");
      if (gfForm) {
        gfForm.addEventListener("submit", (e) => {
          e.preventDefault();
          const family = previewGoogleFontInput();
          if (!family) return;
          if (!savedGoogleFonts.some((item) => item.toLowerCase() === family.toLowerCase())) {
            savedGoogleFonts = [...savedGoogleFonts, family];
            persistSavedGoogleFonts(savedGoogleFonts);
          }
          renderFontPopover();
          closeGoogleFontModal();
          const cssFam = family.includes(" ") ? `'${family}', sans-serif` : `${family}, sans-serif`;
          applyInlineSpanStyle("fontFamily", cssFam);
          const lbl = document.getElementById("rte-font-trigger-label");
          if (lbl) lbl.textContent = family;
          showToast(`✅ Đã thêm Google Font "${family}".`);
        });
      }
      const gfPreviewBtn = document.getElementById("btn-preview-google-font");
      if (gfPreviewBtn) gfPreviewBtn.addEventListener("click", previewGoogleFontInput);
      const gfCloseBtn = document.getElementById("btn-close-google-font-modal");
      if (gfCloseBtn) gfCloseBtn.addEventListener("click", closeGoogleFontModal);
      const gfCancelBtn = document.getElementById("btn-cancel-google-font");
      if (gfCancelBtn) gfCancelBtn.addEventListener("click", closeGoogleFontModal);
      const gfModal = document.getElementById("google-font-modal");
      if (gfModal) gfModal.addEventListener("click", (e) => { if (e.target === gfModal) closeGoogleFontModal(); });

      const blockSelect = document.getElementById("rte-block-format");
      if (blockSelect) {
        blockSelect.addEventListener("change", () => {
          restoreSelection();
          document.execCommand("formatBlock", false, blockSelect.value || "p");
          if (editor) normalizeEditorDom(editor);
          updateCharCount();
        });
      }

      // Arbitrary Font Size 10-72px + - / + buttons preserving savedRange
      const sizeInput = document.getElementById("rte-font-size");
      const decBtn = document.getElementById("rte-fontsize-dec");
      const incBtn = document.getElementById("rte-fontsize-inc");
      if (sizeInput) {
        sizeInput.addEventListener("mousedown", () => saveSelection());
        sizeInput.addEventListener("focus", () => {
          saveSelection();
          sizeInput.select();
        });
        sizeInput.addEventListener("keydown", (event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            applyCustomFontSize(sizeInput.value);
          } else if (event.key === "Escape") {
            event.preventDefault();
            restoreSelection();
            scheduleToolbarSync();
          }
        });
        sizeInput.addEventListener("change", () => applyCustomFontSize(sizeInput.value));
      }
      const stepSize = (delta) => {
        saveSelection();
        const cur = parseInt(sizeInput?.value || "14", 10);
        const base = Number.isNaN(cur) ? 14 : cur;
        applyCustomFontSize(String(Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, base + delta))));
      };
      if (decBtn) decBtn.addEventListener("click", () => stepSize(-1));
      if (incBtn) incBtn.addEventListener("click", () => stepSize(1));

      const colorInput = document.getElementById("rte-text-color");
      if (colorInput) {
        colorInput.addEventListener("change", () => {
          applyInlineSpanStyle("color", colorInput.value);
        });
      }

      // Hyperlink Builder nội tuyến (preset tuyến nội bộ + https:// tự động gắn rel="noopener noreferrer")
      const linkBuilder = document.getElementById("rte-link-builder");
      const toggleLinkBtn = document.getElementById("btn-rte-toggle-link");
      const presetSelect = document.getElementById("rte-link-preset");
      const urlInput = document.getElementById("rte-link-url");
      const textInput = document.getElementById("rte-link-text");
      const applyLinkBtn = document.getElementById("btn-rte-apply-link");
      const cancelLinkBtn = document.getElementById("btn-rte-cancel-link");

      if (toggleLinkBtn && linkBuilder) {
        toggleLinkBtn.addEventListener("click", () => {
          saveSelection();
          linkBuilder.classList.toggle("hidden");
          if (!linkBuilder.classList.contains("hidden") && urlInput) {
            urlInput.focus();
          }
        });
      }
      if (presetSelect && urlInput) {
        presetSelect.addEventListener("change", () => {
          if (presetSelect.value) urlInput.value = presetSelect.value;
        });
      }
      if (cancelLinkBtn && linkBuilder) {
        cancelLinkBtn.addEventListener("click", () => linkBuilder.classList.add("hidden"));
      }
      if (applyLinkBtn && urlInput && linkBuilder) {
        applyLinkBtn.addEventListener("click", () => {
          const href = urlInput.value.trim();
          if (!SAFE_HREF_RE.test(href)) {
            showToast("Liên kết chỉ hỗ trợ tuyến nội bộ (/...) hoặc URL https:// hợp lệ.", false);
            return;
          }
          restoreSelection();
          const sel = window.getSelection();
          const label = (textInput?.value || "").trim() || (sel && !sel.isCollapsed ? sel.toString() : href);
          const anchor = document.createElement("a");
          anchor.setAttribute("href", href);
          if (/^https:\/\//i.test(href)) {
            anchor.setAttribute("target", "_blank");
            anchor.setAttribute("rel", "noopener noreferrer");
          }
          anchor.textContent = label;

          if (sel && sel.rangeCount > 0) {
            const range = sel.getRangeAt(0);
            range.deleteContents();
            range.insertNode(anchor);
            range.setStartAfter(anchor);
            range.collapse(true);
            sel.removeAllRanges();
            sel.addRange(range);
          } else if (editor) {
            editor.appendChild(anchor);
          }

          urlInput.value = "";
          if (textInput) textInput.value = "";
          if (presetSelect) presetSelect.value = "";
          linkBuilder.classList.add("hidden");
          updateCharCount();
        });
      }
    }

    const reloadBtn = document.getElementById("btn-reload-legal");
    if (reloadBtn) reloadBtn.addEventListener("click", () => loadPage(currentDocKey));

    const saveBtn = document.getElementById("btn-save-legal");
    if (saveBtn && isAdmin) {
      saveBtn.addEventListener("click", async () => {
        // BẮT BUỘC chuẩn hóa và đọc DOM đồng bộ ngay trước khi gửi PUT
        if (editor) normalizeEditorDom(editor);
        const titleVal = (document.getElementById("legal-doc-title")?.value || "").trim();
        const subVal = (document.getElementById("legal-doc-subtitle")?.value || "").trim();
        const verVal = (document.getElementById("legal-tos-version")?.value || "").trim();
        const bodyHtml = editor ? editor.innerHTML.trim() : "";

        if (!TOS_VERSION_RE.test(verVal)) {
          showToast("Phiên bản ToS phải theo định dạng vX.Y hoặc vX.Y.Z (ví dụ: v1.0, v1.1).", false);
          return;
        }

        const nextSnapshot = {
          "legal.active_tos_version": verVal,
          [`legal.docs.${currentDocKey}.title`]: titleVal,
          [`legal.docs.${currentDocKey}.subtitle`]: subVal,
          [`legal.docs.${currentDocKey}.body`]: bodyHtml,
        };
        const isBump = verVal !== activeTosVersion;

        await confirmSaveChanges({
          title: isBump
            ? `Xác nhận lưu /${currentDocKey} & nâng ToS lên ${verVal}`
            : `Xác nhận lưu nội dung Rich-Text /${currentDocKey}`,
          subtitle: "Nội dung sẽ được lọc allowlist chống XSS trên server, lưu MongoDB và ghi vào Nhật ký chỉnh sửa",
          before: initialSnapshot || {},
          after: nextSnapshot,
          confirmLabel: isBump ? "Xác nhận nâng phiên bản ToS" : "Xác nhận lưu trang",
          confirmTone: isBump ? "danger" : "primary",
          onConfirm: async (changes) => {
            const savedRes = await apiRequest("/api/legal", {
              method: "PUT",
              cache: "no-store",
              body: JSON.stringify({
                doc_key: currentDocKey,
                title: titleVal,
                subtitle: subVal,
                body: bodyHtml,
                active_tos_version: verVal,
                changes,
              }),
            });
            // Cập nhật trực tiếp trạng thái chuẩn từ phản hồi PUT vừa lưu
            if (savedRes && savedRes.doc && editor) {
              const canonicalHtml = sanitizePreviewHtml(savedRes.doc.body || "");
              loadGoogleFontsUsedInHtml(canonicalHtml);
              editor.innerHTML = canonicalHtml;
              normalizeEditorDom(editor);
              updateCharCount();
            }
            showToast(
              isBump
                ? `✅ Đã nâng phiên bản ToS lên ${verVal} và lưu trang /${currentDocKey}!`
                : `✅ Đã lưu nội dung Rich-Text cho /${currentDocKey}!`
            );
            await Promise.all([loadPage(currentDocKey), loadAcceptances("0")]);
          },
        });
      });
    }

    // Bộ tìm kiếm & phân trang cursor cho Danh sách chấp thuận ToS
    const searchInput = document.getElementById("tos-accept-search");
    const searchBtn = document.getElementById("btn-search-tos-accept");
    const refreshBtn = document.getElementById("btn-refresh-tos-accept");
    const triggerSearch = () => {
      currentSearch = searchInput ? searchInput.value.trim() : "";
      loadAcceptances("0");
    };
    if (searchBtn) searchBtn.addEventListener("click", triggerSearch);
    if (searchInput) {
      searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          triggerSearch();
        }
      });
    }
    if (refreshBtn) refreshBtn.addEventListener("click", () => loadAcceptances(currentCursor));

    const prevBtn = document.getElementById("btn-tos-prev");
    const nextBtn = document.getElementById("btn-tos-next");
    if (prevBtn) {
      prevBtn.addEventListener("click", () => {
        if (prevCursor !== null && prevCursor !== undefined) loadAcceptances(prevCursor);
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", () => {
        if (nextCursor !== null && nextCursor !== undefined) loadAcceptances(nextCursor);
      });
    }

    // Xem trước trực tiếp & lưu mẫu tin nhắn yêu cầu đồng ý ToS trên Discord
    const previewWelcomeBtn = document.getElementById("btn-discord-preview-welcome");
    const previewUpdateBtn = document.getElementById("btn-discord-preview-update");
    if (previewWelcomeBtn) {
      previewWelcomeBtn.addEventListener("click", () => {
        discordPreviewMode = "welcome";
        renderDiscordPromptPreview();
      });
    }
    if (previewUpdateBtn) {
      previewUpdateBtn.addEventListener("click", () => {
        discordPreviewMode = "update";
        renderDiscordPromptPreview();
      });
    }
    ["legal-discord-welcome-tpl", "legal-discord-update-tpl", "legal-discord-btn-label", "legal-tos-version"].forEach(
      (id) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("input", renderDiscordPromptPreview);
      }
    );

    const saveDiscordPromptBtn = document.getElementById("btn-save-discord-prompt");
    if (saveDiscordPromptBtn && isAdmin) {
      saveDiscordPromptBtn.addEventListener("click", async () => {
        const welcomeVal = (document.getElementById("legal-discord-welcome-tpl")?.value || "").trim();
        const updateVal = (document.getElementById("legal-discord-update-tpl")?.value || "").trim();
        const labelVal = (document.getElementById("legal-discord-btn-label")?.value || "").trim();
        if (welcomeVal.length < 10 || updateVal.length < 10) {
          showToast("Mỗi mẫu tin nhắn Discord phải có ít nhất 10 ký tự.", false);
          return;
        }
        if (!labelVal || labelVal.length > 60) {
          showToast("Nhãn nút đồng ý phải từ 1 đến 60 ký tự.", false);
          return;
        }

        const nextPromptSnapshot = {
          "legal.discord_tos_welcome_template": welcomeVal,
          "legal.discord_tos_update_template": updateVal,
          "legal.discord_tos_button_label": labelVal,
        };
        await confirmSaveChanges({
          title: "Xác nhận lưu mẫu tin nhắn ToS trên Discord",
          subtitle: "Áp dụng nóng cho mọi thông báo ephemeral yêu cầu đồng ý ToS và ghi vào Nhật ký chỉnh sửa",
          before: initialDiscordPromptSnapshot || {},
          after: nextPromptSnapshot,
          confirmLabel: "Xác nhận lưu mẫu Discord",
          onConfirm: async (changes) => {
            await apiRequest("/api/legal/discord-prompt", {
              method: "PUT",
              body: JSON.stringify({
                discord_tos_welcome_template: welcomeVal,
                discord_tos_update_template: updateVal,
                discord_tos_button_label: labelVal,
                changes,
              }),
            });
            initialDiscordPromptSnapshot = { ...nextPromptSnapshot };
            showToast("✅ Đã lưu mẫu tin nhắn đồng ý ToS trên Discord!");
          },
        });
      });
    }

    // Ủy quyền sự kiện cho nút Thu hồi (Revoke) trên từng dòng Danh sách chấp thuận ToS
    const acceptTbody = document.getElementById("tos-accept-tbody");
    if (acceptTbody && isAdmin) {
      acceptTbody.addEventListener("click", async (event) => {
        const btn = event.target.closest("button[data-revoke-tos-uid]");
        if (!btn) return;
        const uid = btn.dataset.revokeTosUid || "";
        const prevVer = btn.dataset.revokeTosVer || "—";
        const displayName = btn.dataset.revokeTosName || uid;
        if (!uid) return;

        await showConfirmModal({
          title: `Thu hồi chấp thuận ToS của ${displayName}`,
          subtitle: "Người dùng sẽ bị yêu cầu bấm đồng ý Điều khoản lại ở lượt dùng /ask, phiên AI hoặc /me tiếp theo",
          warning: `Hành động này xóa ngay bản ghi chấp thuận phiên bản ${prevVer} của ID ${uid} khỏi MongoDB và bộ đệm RAM (ghi Audit Log TOS_CONSENT_REVOKED).`,
          changes: [
            {
              key: `tos_acceptances.${uid}.tos_accepted_version`,
              label: `Trạng thái đồng ý ToS (${uid})`,
              before: prevVer,
              after: "(Đã thu hồi — cần đồng ý lại)",
              isHighImpact: true,
            },
          ],
          confirmLabel: "Xác nhận thu hồi",
          confirmTone: "danger",
          onConfirm: async () => {
            await apiRequest(`/api/legal/acceptances/${encodeURIComponent(uid)}/revoke`, {
              method: "POST",
            });
            showToast(`🔄 Đã thu hồi chấp thuận ToS của ${displayName}.`);
            await loadAcceptances(currentCursor);
          },
        });
      });
    }
  }

  async function onTabActivated() {
    ensureEditorInitialized();
    await Promise.all([loadPage(currentDocKey), loadAcceptances("0")]);
  }

  return { onTabActivated, loadPage, loadAcceptances };
}

/**
 * Cổng đồng thuận Điều khoản toàn màn hình trên Web Dashboard.
 */
export async function initWebTosConsentGate() {
  const modal = document.getElementById("tos-consent-modal");
  const panel = document.getElementById("tos-consent-panel");
  const acceptBtn = document.getElementById("btn-accept-tos-web");
  const versionEl = document.getElementById("tos-consent-version");
  const descEl = document.getElementById("tos-consent-desc");
  if (!modal || !panel || !acceptBtn) return;

  try {
    const status = await apiRequest("/api/legal/consent-status");
    if (!status.requires_consent) return;

    if (versionEl) versionEl.textContent = status.active_tos_version || "v1.0";
    if (descEl) {
      descEl.textContent = status.is_update
        ? `Điều khoản Sử dụng đã được nâng cấp lên phiên bản ${status.active_tos_version} (phiên bản trước: ${status.accepted_version}). Vui lòng xem lại và xác nhận để tiếp tục.`
        : `Trước khi tiếp tục sử dụng bảng điều khiển KiwiKiwi, vui lòng đọc và đồng ý Điều khoản Sử dụng phiên bản ${status.active_tos_version}.`;
    }

    openModalAnimated(modal, panel);

    acceptBtn.addEventListener(
      "click",
      async () => {
        acceptBtn.disabled = true;
        acceptBtn.textContent = "Đang ghi nhận...";
        try {
          await apiRequest("/api/legal/accept", {
            method: "POST",
            body: JSON.stringify({ source: "web" }),
          });
          closeModalAnimated(modal, panel);
          showToast(`✅ Đã xác nhận Điều khoản Sử dụng ${status.active_tos_version}!`);
        } catch (err) {
          acceptBtn.disabled = false;
          acceptBtn.textContent = "✅ Tôi đồng ý (Accept Terms)";
          showToast(err.message || "Không ghi nhận được đồng thuận ToS.", false);
        }
      },
      { once: true }
    );
  } catch {
    // Bỏ qua nếu không kiểm tra được trạng thái đồng thuận
  }
}
