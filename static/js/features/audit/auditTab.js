/**
 * Bộ điều khiển Tab 6: Tải và hiển thị nhật ký chỉnh sửa (Audit Log) theo chuyên mục.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

export function createAuditTabController() {
  async function loadAuditLogs() {
    try {
      const sectionEl = document.getElementById("audit-section-filter");
      const section = sectionEl ? sectionEl.value : "";
      const query = section ? `?limit=100&section=${encodeURIComponent(section)}` : "?limit=100";
      const data = await apiRequest(`/api/audit-logs${query}`);
      const tbody = document.getElementById("audit-tbody");
      if (!tbody) return;

      tbody.innerHTML = data.items.length
        ? data.items
            .map((item) => {
              const timeStr = item.at ? new Date(item.at).toLocaleString("vi-VN") : "—";
              const statusBadge = item.pending
                ? '<span class="px-2 py-0.5 rounded-full text-[10px] bg-amber-500/15 text-amber-300 border border-amber-500/30">Chờ sync</span>'
                : '<span class="px-2 py-0.5 rounded-full text-[10px] bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">Atlas</span>';
              return `
                <tr class="hover:bg-[#181d28]/70">
                  <td class="py-2.5 pr-3 font-mono text-[#8b95a7] whitespace-nowrap">${escapeHtml(timeStr)}</td>
                  <td class="py-2.5 pr-3 text-slate-200">${escapeHtml(item.actor || "system")}</td>
                  <td class="py-2.5 pr-3"><span class="px-2 py-0.5 rounded bg-[#181d27] border border-[#252b38] font-mono text-[10px] text-slate-300">${escapeHtml(item.section || "—")}</span></td>
                  <td class="py-2.5 pr-3 text-slate-300">${escapeHtml(item.summary || item.action || "—")}</td>
                  <td class="py-2.5 text-right">${statusBadge}</td>
                </tr>
              `;
            })
            .join("")
        : '<tr><td colspan="5" class="py-4 text-center text-[#5e6778]">Chưa có bản ghi nhật ký chỉnh sửa nào.</td></tr>';
    } catch (err) {
      showToast(err.message, false);
    }
  }

  function bindEvents() {
    const filterEl = document.getElementById("audit-section-filter");
    if (filterEl) filterEl.addEventListener("change", loadAuditLogs);
    const refreshBtn = document.getElementById("btn-refresh-audit");
    if (refreshBtn) refreshBtn.addEventListener("click", loadAuditLogs);
  }

  return { loadAuditLogs, bindEvents };
}
