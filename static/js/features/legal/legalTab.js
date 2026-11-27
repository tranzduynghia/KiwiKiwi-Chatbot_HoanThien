/**
 * Bộ điều khiển Tab "Điều khoản & ToS" (Legal CMS + Acceptance Directory theo múi giờ IP)
 * và Cổng đồng thuận Web toàn màn hình (`initWebTosConsentGate`).
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";
import { closeModalAnimated, openModalAnimated } from "../../core/modalTransition.js";

const TOS_VERSION_RE = /^v\d+\.\d+(?:\.\d+)?$/;

function detectBrowserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

export function createLegalTabController({ isAdmin } = {}) {
  let docsMap = {};
  let activeTosVersion = "v1.0";
  let currentDocKey = "terms";
  let initialDocSnapshot = null;

  let currentCursor = "0";
  let nextCursor = null;
  let prevCursor = null;
  let currentSearch = "";

  function renderLivePreview() {
    const titleEl = document.getElementById("legal-doc-title");
    const subtitleEl = document.getElementById("legal-doc-subtitle");
    const bodyEl = document.getElementById("legal-doc-body");
    const verEl = document.getElementById("legal-tos-version");
    const charEl = document.getElementById("legal-body-chars");

    const pTitle = document.getElementById("legal-preview-title");
    const pSub = document.getElementById("legal-preview-subtitle");
    const pBody = document.getElementById("legal-preview-body");
    const pVer = document.getElementById("legal-preview-version");
    const pPath = document.getElementById("legal-preview-path");

    const titleVal = titleEl ? titleEl.value.trim() : "";
    const subVal = subtitleEl ? subtitleEl.value.trim() : "";
    const bodyVal = bodyEl ? bodyEl.value : "";
    const verVal = verEl ? verEl.value.trim() : activeTosVersion;

    if (charEl) charEl.textContent = `${bodyVal.length.toLocaleString("vi-VN")} ký tự`;
    if (pTitle) pTitle.textContent = titleVal || "(Chưa có tiêu đề)";
    if (pSub) pSub.textContent = subVal;
    if (pBody) pBody.textContent = bodyVal;
    if (pVer) pVer.textContent = verVal || activeTosVersion;
    if (pPath) pPath.textContent = `/${currentDocKey}`;
  }

  function populateEditorFor(docKey) {
    currentDocKey = docKey;
    const doc = docsMap[docKey] || { title: "", subtitle: "", body: "" };
    const titleEl = document.getElementById("legal-doc-title");
    const subtitleEl = document.getElementById("legal-doc-subtitle");
    const bodyEl = document.getElementById("legal-doc-body");
    const verEl = document.getElementById("legal-tos-version");
    const selectEl = document.getElementById("legal-doc-select");

    if (selectEl && selectEl.value !== docKey) selectEl.value = docKey;
    if (titleEl) titleEl.value = doc.title || "";
    if (subtitleEl) subtitleEl.value = doc.subtitle || "";
    if (bodyEl) bodyEl.value = doc.body || "";
    if (verEl) verEl.value = activeTosVersion;

    initialDocSnapshot = {
      "legal.active_tos_version": activeTosVersion,
      [`legal.docs.${docKey}.title`]: doc.title || "",
      [`legal.docs.${docKey}.subtitle`]: doc.subtitle || "",
      [`legal.docs.${docKey}.body`]: doc.body || "",
    };
    renderLivePreview();
  }

  async function loadLegalDocs() {
    try {
      const tz = detectBrowserTimezone();
      const qs = tz ? `?tz=${encodeURIComponent(tz)}` : "";
      const data = await apiRequest(`/api/legal${qs}`);
      docsMap = data.docs || {};
      activeTosVersion = data.active_tos_version || "v1.0";

      const verBadge = document.getElementById("legal-active-version-badge");
      if (verBadge) verBadge.textContent = `Phiên bản: ${activeTosVersion}`;
      const updBadge = document.getElementById("legal-updated-badge");
      if (updBadge) updBadge.textContent = `Cập nhật: ${data.tos_updated_at_formatted || "—"}`;
      const tzBadge = document.getElementById("legal-timezone-badge");
      if (tzBadge && data.timezone_label) {
        tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      }

      populateEditorFor(currentDocKey);
    } catch (err) {
      showToast("Không tải được nội dung Pháp lý: " + err.message, false);
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

  async function loadAll() {
    await Promise.all([loadLegalDocs(), loadAcceptances("0")]);
  }

  function bindEvents() {
    const selectEl = document.getElementById("legal-doc-select");
    if (selectEl) {
      selectEl.addEventListener("change", () => {
        populateEditorFor(selectEl.value);
      });
    }

    ["legal-doc-title", "legal-doc-subtitle", "legal-doc-body", "legal-tos-version"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("input", renderLivePreview);
    });

    const reloadBtn = document.getElementById("btn-reload-legal");
    if (reloadBtn) {
      reloadBtn.addEventListener("click", () => loadLegalDocs());
    }

    const saveBtn = document.getElementById("btn-save-legal");
    if (saveBtn && isAdmin) {
      saveBtn.addEventListener("click", async () => {
        const titleVal = (document.getElementById("legal-doc-title")?.value || "").trim();
        const subtitleVal = (document.getElementById("legal-doc-subtitle")?.value || "").trim();
        const bodyVal = (document.getElementById("legal-doc-body")?.value || "").trim();
        const verVal = (document.getElementById("legal-tos-version")?.value || "").trim();

        if (!TOS_VERSION_RE.test(verVal)) {
          showToast("Phiên bản ToS phải theo định dạng vX.Y hoặc vX.Y.Z (ví dụ: v1.0, v1.1).", false);
          return;
        }

        const nextSnapshot = {
          "legal.active_tos_version": verVal,
          [`legal.docs.${currentDocKey}.title`]: titleVal,
          [`legal.docs.${currentDocKey}.subtitle`]: subtitleVal,
          [`legal.docs.${currentDocKey}.body`]: bodyVal,
        };
        const isBump = verVal !== activeTosVersion;

        await confirmSaveChanges({
          title: isBump
            ? `Xác nhận cập nhật /${currentDocKey} & nâng ToS lên ${verVal}`
            : `Xác nhận cập nhật tài liệu /${currentDocKey}`,
          subtitle: "Nội dung mới sẽ áp dụng tức thì trên trang công khai và được ghi vào Nhật ký chỉnh sửa",
          before: initialDocSnapshot || {},
          after: nextSnapshot,
          confirmLabel: isBump ? "Xác nhận nâng phiên bản ToS" : "Xác nhận lưu nội dung",
          confirmTone: isBump ? "danger" : "primary",
          onConfirm: async (changes) => {
            await apiRequest("/api/legal", {
              method: "PUT",
              body: JSON.stringify({
                doc_key: currentDocKey,
                title: titleVal,
                subtitle: subtitleVal,
                body: bodyVal,
                active_tos_version: verVal,
                changes,
              }),
            });
            showToast(
              isBump
                ? `✅ Đã nâng phiên bản ToS lên ${verVal} và lưu tài liệu /${currentDocKey}!`
                : `✅ Đã lưu nội dung tài liệu /${currentDocKey}!`
            );
            await loadAll();
          },
        });
      });
    }

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

  return { loadAll, loadLegalDocs, loadAcceptances, bindEvents };
}

/**
 * Cổng đồng thuận Điều khoản toàn màn hình trên Web Dashboard:
 * Tự kiểm tra `tos_accepted_version == active_tos_version` và yêu cầu xác nhận khi phiên bản ToS thay đổi.
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
        ? `Điều khoản Dịch vụ đã được nâng cấp lên phiên bản ${status.active_tos_version} (phiên bản trước: ${status.accepted_version}). Vui lòng xem lại và xác nhận để tiếp tục.`
        : `Trước khi tiếp tục sử dụng bảng điều khiển KiwiKiwi, vui lòng đọc và đồng ý Điều khoản Dịch vụ phiên bản ${status.active_tos_version}.`;
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
          showToast(`✅ Đã xác nhận Điều khoản Dịch vụ ${status.active_tos_version}!`);
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
