/**
 * Bộ điều khiển Tab "Điều khoản & Pháp lý" (5-Page Legal CMS với Rich-Text Editor khởi tạo lười)
 * + Danh sách chấp thuận ToS theo múi giờ IP + Cổng đồng thuận Web toàn màn hình.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";
import { closeModalAnimated, openModalAnimated } from "../../core/modalTransition.js";

const TOS_VERSION_RE = /^v\d+\.\d+(?:\.\d+)?$/;
const SAFE_HREF_RE = /^(\/(?!\/)|https:\/\/)/i;
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

  let currentCursor = "0";
  let nextCursor = null;
  let prevCursor = null;
  let currentSearch = "";

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
    if (!value) return;
    restoreSelection();
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (range.collapsed) {
      const span = document.createElement("span");
      span.style[prop] = value;
      span.innerHTML = "\u200B";
      range.insertNode(span);
      range.setStart(span.firstChild, 1);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    } else {
      const span = document.createElement("span");
      span.style[prop] = value;
      try {
        range.surroundContents(span);
      } catch {
        const extracted = range.extractContents();
        span.appendChild(extracted);
        range.insertNode(span);
      }
    }
    updateCharCount();
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
      if (pBody && editor) pBody.innerHTML = sanitizePreviewHtml(editor.innerHTML);

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
      const data = await apiRequest(`/api/legal?${params.toString()}`);

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
      if (editor) editor.innerHTML = sanitizePreviewHtml(doc.body || "");

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
          '<tr><td colspan="5" class="py-6 text-center text-[#8b95a7]">Chưa có người dùng nào ghi nhận chấp thuận ToS.</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const verBadgeClass = item.is_current_version
            ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/35"
            : "bg-amber-500/15 text-amber-300 border-amber-500/35";
          const verNote = item.is_current_version ? "Hiện hành" : `Cũ (cần ${escapeHtml(data.active_tos_version)})`;
          const sourceBadge =
            item.consent_source === "web"
              ? '<span class="px-2 py-0.5 rounded-full text-[10px] bg-sky-500/15 text-sky-300 border border-sky-500/30">Web</span>'
              : '<span class="px-2 py-0.5 rounded-full text-[10px] bg-indigo-500/15 text-indigo-300 border border-indigo-500/30">Discord</span>';
          return `
            <tr>
              <td class="py-2.5 pr-3 font-mono text-slate-100">${escapeHtml(item.discord_user_id)}</td>
              <td class="py-2.5 pr-3 text-slate-200 font-medium">${escapeHtml(item.discord_username || "—")}</td>
              <td class="py-2.5 pr-3">
                <span class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full border font-mono text-[10px] ${verBadgeClass}">
                  ${escapeHtml(item.tos_accepted_version)} · ${verNote}
                </span>
              </td>
              <td class="py-2.5 pr-3">${sourceBadge}</td>
              <td class="py-2.5 text-right font-mono text-slate-300">${escapeHtml(item.tos_accepted_at_formatted || "—")}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="5" class="py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
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
        saveSelection();
        updateCharCount();
      });
      editor.addEventListener("mouseup", saveSelection);
      editor.addEventListener("input", updateCharCount);
    }

    const toolbar = document.getElementById("legal-rte-toolbar");
    if (toolbar && isAdmin) {
      toolbar.addEventListener("mousedown", (event) => {
        if (event.target.closest("button")) {
          // Giữ nguyên vùng chọn chữ trong contenteditable khi nhấn nút toolbar
          event.preventDefault();
          saveSelection();
        }
      });

      toolbar.addEventListener("click", (event) => {
        const cmdBtn = event.target.closest("button[data-rte-cmd]");
        if (cmdBtn) {
          restoreSelection();
          document.execCommand(cmdBtn.dataset.rteCmd, false, null);
          updateCharCount();
          return;
        }
        const actionBtn = event.target.closest("button[data-rte-action]");
        if (actionBtn && actionBtn.dataset.rteAction === "blockquote") {
          restoreSelection();
          document.execCommand("formatBlock", false, "blockquote");
          updateCharCount();
        }
      });

      const fontSelect = document.getElementById("rte-font-family");
      if (fontSelect) {
        fontSelect.addEventListener("change", () => {
          applyInlineSpanStyle("fontFamily", fontSelect.value);
          fontSelect.value = "";
        });
      }

      const blockSelect = document.getElementById("rte-block-format");
      if (blockSelect) {
        blockSelect.addEventListener("change", () => {
          restoreSelection();
          document.execCommand("formatBlock", false, blockSelect.value || "p");
          updateCharCount();
        });
      }

      const sizeSelect = document.getElementById("rte-font-size");
      if (sizeSelect) {
        sizeSelect.addEventListener("change", () => {
          applyInlineSpanStyle("fontSize", sizeSelect.value);
          sizeSelect.value = "";
        });
      }

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
            await apiRequest("/api/legal", {
              method: "PUT",
              body: JSON.stringify({
                doc_key: currentDocKey,
                title: titleVal,
                subtitle: subVal,
                body: bodyHtml,
                active_tos_version: verVal,
                changes,
              }),
            });
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
