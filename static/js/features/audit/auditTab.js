/**
 * Bộ điều khiển Tab Nhật ký chỉnh sửa (Audit Log):
 * - Hiển thị Avatar Discord (hoặc icon mặc định nếu đăng nhập bằng tài khoản/mật khẩu)
 * - Nút "Chi tiết" ở cuối mỗi dòng mở Modal so sánh Giá trị gốc -> Giá trị mới tại chỗ
 */

import { apiRequest, escapeHtml, sanitizeHttpUrl, showToast } from "../../core/api.js";
import { closeModalAnimated, openModalAnimated } from "../../core/modalTransition.js";

const SENSITIVE_FIELD_TOKENS = [
  "password",
  "pepper",
  "secret",
  "token",
  "api_key",
  "apikey",
  "uri",
  "salt",
  "hash",
  "signing_key",
  "private_key",
  "credential",
];

const SECTION_LABELS = {
  ask: "Cấu hình /ask",
  prompt: "System Instruction",
  models: "Model Gemini",
  presence: "Trạng thái Bot (Presence)",
  tunnel: "Cloudflare Tunnel",
  discord_permissions: "Phân quyền Discord",
  dashboard_users: "Tài khoản Dashboard",
  legal: "Điều khoản & ToS",
  system: "Hệ thống & Bộ nhớ",
  config: "Cấu hình chung",
};

function formatExactTimestamp(isoString) {
  if (!isoString) return "—";
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return String(isoString);
  const pad = (n) => String(n).padStart(2, "0");
  const hh = pad(date.getHours());
  const mm = pad(date.getMinutes());
  const ss = pad(date.getSeconds());
  const dd = pad(date.getDate());
  const mo = pad(date.getMonth() + 1);
  const yyyy = date.getFullYear();
  return `${hh}:${mm}:${ss} ${dd}/${mo}/${yyyy}`;
}

function isSensitiveField(fieldName) {
  const lowered = String(fieldName || "").toLowerCase();
  return SENSITIVE_FIELD_TOKENS.some((tok) => lowered.includes(tok));
}

function formatDiffValue(value, fieldName) {
  if (isSensitiveField(fieldName)) {
    return value === "(trống)" || value === null || value === undefined || value === ""
      ? "(trống)"
      : "••••••••••••";
  }
  if (value === null || value === undefined || value === "") {
    return "(trống)";
  }
  if (typeof value === "boolean") {
    return value ? "Bật (true)" : "Tắt (false)";
  }
  if (typeof value === "object") {
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function renderActorAvatar(item, sizeClass = "w-8 h-8") {
  const displayName = item.actor_display_name || item.actor || "system";
  const safeAvatarUrl = sanitizeHttpUrl(item.actor_avatar_url);
  if (safeAvatarUrl) {
    return `
      <img
        src="${escapeHtml(safeAvatarUrl)}"
        alt="${escapeHtml(displayName)}"
        loading="lazy"
        class="${sizeClass} ox-surface-chip rounded-full object-cover shrink-0"
      />
    `;
  }
  // Icon người dùng mặc định cho tài khoản đăng nhập truyền thống hoặc hệ thống
  return `
    <div
      class="${sizeClass} ox-surface-chip ox-text-secondary rounded-full flex items-center justify-center shrink-0"
      aria-hidden="true"
    >
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
        <circle cx="12" cy="7" r="4" />
      </svg>
    </div>
  `;
}

function renderActorCell(item) {
  const displayName = item.actor_display_name || item.actor || "system";
  const discordId = item.actor_discord_id || "";
  const isDiscord = item.actor_type === "discord" || Boolean(discordId);
  const subLabel = isDiscord
    ? `Discord ID: ${discordId || "—"}`
    : item.source
      ? String(item.source)
      : item.actor_type === "system"
        ? "Hệ thống"
        : "Tài khoản Dashboard";

  return `
    <div class="flex items-center gap-2.5 min-w-0">
      ${renderActorAvatar(item, "w-8 h-8")}
      <div class="min-w-0">
        <div class="font-medium text-slate-100 truncate">${escapeHtml(displayName)}</div>
        <div class="text-[10px] text-[#5e6778] font-mono truncate">${escapeHtml(subLabel)}</div>
      </div>
    </div>
  `;
}

export function createAuditTabController() {
  const PAGE_SIZE = 5;
  let cachedItems = [];
  let currentPage = 1;
  let totalPages = 1;
  let totalItems = 0;
  let lastRenderKey = "";
  let isFetching = false;
  let isNavigating = false;
  let requestSeq = 0;

  function renderSkeletonRows(tbody) {
    tbody.innerHTML = Array.from({ length: PAGE_SIZE })
      .map(
        () => `
          <tr>
            <td class="py-3 pr-3"><span class="ox-skeleton-bar h-3.5 w-28"></span></td>
            <td class="py-3 pr-3">
              <div class="flex items-center gap-2.5">
                <span class="ox-skeleton-bar w-8 h-8 rounded-full shrink-0"></span>
                <div class="space-y-1.5">
                  <span class="ox-skeleton-bar h-3 w-24 block"></span>
                  <span class="ox-skeleton-bar h-2.5 w-20 block"></span>
                </div>
              </div>
            </td>
            <td class="py-3 pr-3"><span class="ox-skeleton-bar h-4 w-24"></span></td>
            <td class="py-3 pr-3"><span class="ox-skeleton-bar h-3.5 w-48"></span></td>
            <td class="py-3 pr-3 text-center"><span class="ox-skeleton-bar h-4 w-12"></span></td>
            <td class="py-3 text-right"><span class="ox-skeleton-bar h-6 w-20"></span></td>
          </tr>
        `
      )
      .join("");
  }

  function updatePaginationControls() {
    const badgeEl = document.getElementById("audit-page-badge");
    const totalEl = document.getElementById("audit-total-count");
    const prevBtn = document.getElementById("btn-audit-prev");
    const nextBtn = document.getElementById("btn-audit-next");

    if (badgeEl) badgeEl.textContent = `Trang ${currentPage} / ${totalPages}`;
    if (totalEl) totalEl.textContent = `Tổng: ${totalItems} bản ghi`;
    if (prevBtn) prevBtn.disabled = currentPage <= 1 || isNavigating;
    if (nextBtn) nextBtn.disabled = currentPage >= totalPages || isNavigating;
  }

  function closeAuditDetailModal() {
    const modal = document.getElementById("audit-detail-modal");
    if (!modal) return;
    closeModalAnimated(modal);
  }

  function openAuditDetail(index) {
    const item = cachedItems[index];
    if (!item) return;

    const modal = document.getElementById("audit-detail-modal");
    const headerEl = document.getElementById("audit-detail-header");
    const metaEl = document.getElementById("audit-detail-meta");
    const diffEl = document.getElementById("audit-detail-diff");
    if (!modal || !headerEl || !metaEl || !diffEl) return;

    const displayName = item.actor_display_name || item.actor || "system";
    const discordId = item.actor_discord_id || "";
    const exactTime = formatExactTimestamp(item.at);
    const sectionName = SECTION_LABELS[item.section] || item.section || "—";
    const changes = Array.isArray(item.changes) ? item.changes : [];

    headerEl.innerHTML = `
      ${renderActorAvatar(item, "w-10 h-10")}
      <div class="min-w-0">
        <div class="flex items-center gap-2 flex-wrap">
          <h3 id="audit-detail-title" class="text-sm font-semibold ox-text-primary truncate">${escapeHtml(displayName)}</h3>
          ${discordId ? `<span class="text-[11px] font-mono text-indigo-300 bg-indigo-500/10 border border-indigo-500/25 px-2 py-0.5 rounded-full">Discord: ${escapeHtml(discordId)}</span>` : ""}
        </div>
        <div class="text-[11px] ox-text-secondary mt-0.5">${escapeHtml(item.summary || item.action || "Thay đổi cấu hình")}</div>
      </div>
    `;

    metaEl.innerHTML = `
      <div class="flex flex-wrap items-center gap-3 text-[11px] ox-text-secondary">
        <span>🕒 Thời gian: <strong class="font-mono ox-text-primary">${escapeHtml(exactTime)}</strong></span>
        <span>📂 Chuyên mục: <strong class="text-rose-300">${escapeHtml(sectionName)}</strong></span>
        <span>📝 Hành động: <code class="font-mono ox-text-primary">${escapeHtml(item.action || "—")}</code></span>
        <span>🔄 Số trường thay đổi: <strong class="font-mono ox-text-primary">${changes.length}</strong></span>
      </div>
    `;

    if (!changes.length) {
      diffEl.innerHTML =
        '<div class="py-8 text-center text-xs ox-text-secondary">Bản ghi này không có chi tiết thay đổi trường dữ liệu.</div>';
    } else {
      diffEl.innerHTML = `
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="ox-diff-thead">
                <th class="py-2 px-3 w-52">Trường cấu hình</th>
                <th class="py-2 px-2">Giá trị gốc</th>
                <th class="py-2 px-1 text-center w-6"></th>
                <th class="py-2 px-2">Giá trị mới</th>
              </tr>
            </thead>
            <tbody class="ox-diff-tbody divide-y">
              ${changes
                .map((ch) => {
                  const field = ch.field || "—";
                  const beforeStr = formatDiffValue(ch.before, field);
                  const afterStr = formatDiffValue(ch.after, field);
                  return `
                    <tr class="align-top ox-diff-row">
                      <td class="py-3 px-3 font-mono ox-text-primary break-all">${escapeHtml(field)}</td>
                      <td class="py-3 px-2">
                        <div class="ox-alert-danger rounded-md px-2.5 py-1.5 font-mono text-[11px] whitespace-pre-wrap break-words max-h-40 overflow-y-auto">${escapeHtml(beforeStr)}</div>
                      </td>
                      <td class="py-3 px-1 text-center ox-text-secondary font-bold">→</td>
                      <td class="py-3 px-2">
                        <div class="ox-diff-after-box rounded-md px-2.5 py-1.5 font-mono text-[11px] whitespace-pre-wrap break-words max-h-40 overflow-y-auto">${escapeHtml(afterStr)}</div>
                      </td>
                    </tr>
                  `;
                })
                .join("")}
            </tbody>
          </table>
        </div>
      `;
    }

    openModalAnimated(modal);
  }

  async function loadAuditLogs({ silent = false, page = null, animateTransition = false } = {}) {
    if ((isFetching || isNavigating) && silent) return;
    const targetPage = Math.max(1, page !== null ? Number(page) : currentPage);
    const tbody = document.getElementById("audit-tbody");
    if (!tbody) return;

    const seq = ++requestSeq;
    isFetching = true;
    if (!silent) {
      isNavigating = true;
      updatePaginationControls();
    }

    try {
      // Hiệu ứng mờ dần/trượt nhẹ trang cũ + skeleton giữ chiều cao ổn định khi người dùng chủ động chuyển trang
      if (!silent && animateTransition) {
        tbody.classList.add("ox-audit-leaving");
        await new Promise((resolve) => setTimeout(resolve, 130));
        if (seq !== requestSeq) return;
        tbody.classList.remove("ox-audit-leaving");
        renderSkeletonRows(tbody);
      } else if (!silent && !cachedItems.length) {
        renderSkeletonRows(tbody);
      }

      const sectionEl = document.getElementById("audit-section-filter");
      const section = sectionEl ? sectionEl.value : "";
      const params = new URLSearchParams({
        page: String(targetPage),
        limit: String(PAGE_SIZE),
      });
      if (section) params.set("section", section);

      const data = await apiRequest(`/api/audit-logs?${params.toString()}`);
      // Bỏ qua phản hồi cũ nếu người dùng đã chuyển sang trang/bộ lọc mới hơn
      if (seq !== requestSeq) return;

      const items = data.items || [];
      currentPage = Number(data.page || targetPage);
      totalPages = Math.max(1, Number(data.total_pages || 1));
      totalItems = Number(data.total_items ?? items.length);

      const nextKey = JSON.stringify({
        page: currentPage,
        totalItems,
        totalPages,
        items,
      });

      // Khi cập nhật nền im lặng 1s và trang hiện tại không thay đổi -> giữ nguyên DOM, không chạy lại animation
      if (silent && nextKey === lastRenderKey) {
        updatePaginationControls();
        return;
      }

      cachedItems = items;
      lastRenderKey = nextKey;
      const rowAnimClass = silent ? "" : "ox-audit-row-enter";

      tbody.innerHTML = cachedItems.length
        ? cachedItems
            .map((item, idx) => {
              const timeStr = formatExactTimestamp(item.at);
              const sectionName = SECTION_LABELS[item.section] || item.section || "—";
              const changeCount = Array.isArray(item.changes) ? item.changes.length : 0;
              const statusBadge = item.pending
                ? '<span class="ox-badge-amber px-2 py-0.5 rounded-full text-[10px] font-medium">Chờ sync</span>'
                : '<span class="ox-badge-emerald px-2 py-0.5 rounded-full text-[10px] font-medium">Atlas</span>';
              return `
                <tr class="ox-table-row ${rowAnimClass}" style="--row-idx:${idx}">
                  <td class="py-2.5 pr-3 font-mono ox-text-secondary whitespace-nowrap">${escapeHtml(timeStr)}</td>
                  <td class="py-2.5 pr-3">${renderActorCell(item)}</td>
                  <td class="py-2.5 pr-3"><span class="ox-badge-neutral px-2 py-0.5 rounded text-[10px] whitespace-nowrap">${escapeHtml(sectionName)}</span></td>
                  <td class="py-2.5 pr-3 ox-text-secondary">${escapeHtml(item.summary || item.action || "—")}</td>
                  <td class="py-2.5 pr-3 text-center">${statusBadge}</td>
                  <td class="py-2.5 text-right whitespace-nowrap">
                    <button
                      type="button"
                      data-audit-detail-idx="${idx}"
                      class="btn-ghost text-[11px] px-2.5 py-1"
                    >👁 Chi tiết${changeCount ? ` (${changeCount})` : ""}</button>
                  </td>
                </tr>
              `;
            })
            .join("")
        : '<tr><td colspan="6" class="py-12 text-center ox-text-secondary">Chưa có bản ghi nhật ký chỉnh sửa nào.</td></tr>';
    } catch (err) {
      if (seq === requestSeq && !silent) {
        const msg = err?.message || "Không thể tải danh sách nhật ký.";
        showToast(msg, false);
        tbody.innerHTML = `<tr><td colspan="6" class="py-12 text-center text-rose-300">Không tải được Nhật ký chỉnh sửa: ${escapeHtml(msg)}</td></tr>`;
      }
    } finally {
      if (seq === requestSeq) {
        isFetching = false;
        isNavigating = false;
        updatePaginationControls();
      }
    }
  }

  function bindEvents() {
    const filterEl = document.getElementById("audit-section-filter");
    if (filterEl) {
      filterEl.addEventListener("change", () => {
        currentPage = 1;
        loadAuditLogs({ silent: false, page: 1, animateTransition: true });
      });
    }
    const refreshBtn = document.getElementById("btn-refresh-audit");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => {
        loadAuditLogs({ silent: false, page: currentPage, animateTransition: true });
      });
    }

    const prevBtn = document.getElementById("btn-audit-prev");
    const nextBtn = document.getElementById("btn-audit-next");
    if (prevBtn) {
      prevBtn.addEventListener("click", () => {
        if (currentPage <= 1 || isNavigating) return;
        loadAuditLogs({ silent: false, page: currentPage - 1, animateTransition: true });
      });
    }
    if (nextBtn) {
      nextBtn.addEventListener("click", () => {
        if (currentPage >= totalPages || isNavigating) return;
        loadAuditLogs({ silent: false, page: currentPage + 1, animateTransition: true });
      });
    }

    // Mở Modal chi tiết diff tại chỗ khi bấm nút "Chi tiết"
    const tbody = document.getElementById("audit-tbody");
    if (tbody) {
      tbody.addEventListener("click", (event) => {
        const btn = event.target.closest("button[data-audit-detail-idx]");
        if (!btn) return;
        event.preventDefault();
        openAuditDetail(Number(btn.dataset.auditDetailIdx));
      });
    }

    // Đóng Modal chi tiết bằng nút ×, bấm nền tối hoặc phím Escape
    const closeBtn = document.getElementById("btn-close-audit-detail");
    if (closeBtn) closeBtn.addEventListener("click", closeAuditDetailModal);
    const modal = document.getElementById("audit-detail-modal");
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeAuditDetailModal();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeAuditDetailModal();
    });
  }

  return { loadAuditLogs, bindEvents };
}
