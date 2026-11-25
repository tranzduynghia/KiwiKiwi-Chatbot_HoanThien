/**
 * Bộ điều khiển Tab Nhật ký chỉnh sửa (Audit Log):
 * - Hiển thị Avatar Discord (hoặc icon mặc định nếu đăng nhập bằng tài khoản/mật khẩu)
 * - Nút "Chi tiết" ở cuối mỗi dòng mở Modal so sánh Giá trị gốc -> Giá trị mới tại chỗ
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

const SENSITIVE_FIELD_TOKENS = ["password", "secret", "token", "api_key", "apikey", "uri"];

const SECTION_LABELS = {
  ask: "Cấu hình /ask",
  prompt: "System Instruction",
  models: "Model Gemini",
  discord_permissions: "Phân quyền Discord",
  dashboard_users: "Tài khoản Dashboard",
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
    return "•••••••• (đã ẩn)";
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
  if (item.actor_avatar_url) {
    return `
      <img
        src="${escapeHtml(item.actor_avatar_url)}"
        alt="${escapeHtml(displayName)}"
        loading="lazy"
        class="${sizeClass} rounded-full object-cover border border-[#2a3140] shrink-0 bg-[#0a0d12]"
      />
    `;
  }
  // Icon người dùng mặc định cho tài khoản đăng nhập truyền thống hoặc hệ thống
  return `
    <div
      class="${sizeClass} rounded-full bg-[#181d27] border border-[#2a3140] text-[#8b95a7] flex items-center justify-center shrink-0"
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
  let cachedItems = [];
  let lastRenderKey = "";
  let isFetching = false;

  function closeAuditDetailModal() {
    const modal = document.getElementById("audit-detail-modal");
    if (!modal) return;
    modal.classList.add("hidden");
    modal.classList.remove("flex");
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
          <h3 id="audit-detail-title" class="text-sm font-semibold text-white truncate">${escapeHtml(displayName)}</h3>
          ${discordId ? `<span class="text-[11px] font-mono text-indigo-300 bg-indigo-500/10 border border-indigo-500/25 px-2 py-0.5 rounded-full">Discord: ${escapeHtml(discordId)}</span>` : ""}
        </div>
        <div class="text-[11px] text-[#8b95a7] mt-0.5">${escapeHtml(item.summary || item.action || "Thay đổi cấu hình")}</div>
      </div>
    `;

    metaEl.innerHTML = `
      <div class="flex flex-wrap items-center gap-3 text-[11px] text-[#8b95a7]">
        <span>🕒 Thời gian: <strong class="font-mono text-slate-200">${escapeHtml(exactTime)}</strong></span>
        <span>📂 Chuyên mục: <strong class="text-rose-300">${escapeHtml(sectionName)}</strong></span>
        <span>📝 Hành động: <code class="font-mono text-slate-300">${escapeHtml(item.action || "—")}</code></span>
        <span>🔄 Số trường thay đổi: <strong class="font-mono text-white">${changes.length}</strong></span>
      </div>
    `;

    if (!changes.length) {
      diffEl.innerHTML =
        '<div class="py-8 text-center text-xs text-[#5e6778]">Bản ghi này không có chi tiết thay đổi trường dữ liệu.</div>';
    } else {
      diffEl.innerHTML = `
        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[#1e232e] text-[#8b95a7]">
                <th class="py-2 pr-3 w-52">Trường cấu hình</th>
                <th class="py-2 pr-2">Giá trị gốc</th>
                <th class="py-2 px-1 text-center w-6"></th>
                <th class="py-2 pl-2">Giá trị mới</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-[#1e232e]/80">
              ${changes
                .map((ch) => {
                  const field = ch.field || "—";
                  const beforeStr = formatDiffValue(ch.before, field);
                  const afterStr = formatDiffValue(ch.after, field);
                  return `
                    <tr class="align-top hover:bg-[#131720]/70">
                      <td class="py-3 pr-3 font-mono text-slate-200 break-all">${escapeHtml(field)}</td>
                      <td class="py-3 pr-2">
                        <div class="rounded-md bg-rose-500/10 border border-rose-500/25 px-2.5 py-1.5 font-mono text-[11px] text-rose-200 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">${escapeHtml(beforeStr)}</div>
                      </td>
                      <td class="py-3 px-1 text-center text-[#5e6778] font-bold">→</td>
                      <td class="py-3 pl-2">
                        <div class="rounded-md bg-emerald-500/10 border border-emerald-500/25 px-2.5 py-1.5 font-mono text-[11px] text-emerald-200 whitespace-pre-wrap break-words max-h-40 overflow-y-auto">${escapeHtml(afterStr)}</div>
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

    const panel = modal.firstElementChild;
    if (panel) {
      panel.classList.remove("ox-modal-enter");
      void panel.offsetWidth;
      panel.classList.add("ox-modal-enter");
    }
    modal.classList.remove("hidden");
    modal.classList.add("flex");
  }

  async function loadAuditLogs({ silent = false } = {}) {
    if (isFetching && silent) return;
    isFetching = true;
    try {
      const sectionEl = document.getElementById("audit-section-filter");
      const section = sectionEl ? sectionEl.value : "";
      const query = section ? `?limit=100&section=${encodeURIComponent(section)}` : "?limit=100";
      const data = await apiRequest(`/api/audit-logs${query}`);
      const tbody = document.getElementById("audit-tbody");
      if (!tbody) return;

      const items = data.items || [];
      const nextKey = JSON.stringify(items);
      // Nếu đang cập nhật nền im lặng và dữ liệu không đổi -> giữ nguyên DOM & vị trí cuộn
      if (silent && nextKey === lastRenderKey) {
        return;
      }

      cachedItems = items;
      lastRenderKey = nextKey;
      const rowAnimClass = silent ? "" : "ox-enter";

      tbody.innerHTML = cachedItems.length
        ? cachedItems
            .map((item, idx) => {
              const timeStr = formatExactTimestamp(item.at);
              const sectionName = SECTION_LABELS[item.section] || item.section || "—";
              const changeCount = Array.isArray(item.changes) ? item.changes.length : 0;
              const statusBadge = item.pending
                ? '<span class="px-2 py-0.5 rounded-full text-[10px] bg-amber-500/15 text-amber-300 border border-amber-500/30">Chờ sync</span>'
                : '<span class="px-2 py-0.5 rounded-full text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">Atlas</span>';
              return `
                <tr class="hover:bg-[#181d28]/70 ${rowAnimClass}">
                  <td class="py-2.5 pr-3 font-mono text-[#8b95a7] whitespace-nowrap">${escapeHtml(timeStr)}</td>
                  <td class="py-2.5 pr-3">${renderActorCell(item)}</td>
                  <td class="py-2.5 pr-3"><span class="px-2 py-0.5 rounded bg-[#181d27] border border-[#252b38] text-[10px] text-slate-300 whitespace-nowrap">${escapeHtml(sectionName)}</span></td>
                  <td class="py-2.5 pr-3 text-slate-300">${escapeHtml(item.summary || item.action || "—")}</td>
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
        : '<tr><td colspan="6" class="py-4 text-center text-[#5e6778]">Chưa có bản ghi nhật ký chỉnh sửa nào.</td></tr>';
    } catch (err) {
      if (!silent) showToast(err.message, false);
    } finally {
      isFetching = false;
    }
  }

  function bindEvents() {
    const filterEl = document.getElementById("audit-section-filter");
    if (filterEl) filterEl.addEventListener("change", () => loadAuditLogs({ silent: false }));
    const refreshBtn = document.getElementById("btn-refresh-audit");
    if (refreshBtn) refreshBtn.addEventListener("click", () => loadAuditLogs({ silent: false }));

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
