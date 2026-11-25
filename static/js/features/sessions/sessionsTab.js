/**
 * Bộ điều khiển Tab Tra cứu phiên chat: tìm kiếm nội dung tin nhắn trực tiếp từ MongoDB Atlas
 * và hiển thị 3 cột: Mã chat (Chat ID), Nội dung tin nhắn và Thời gian.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

function highlightMatch(rawText, keyword) {
  const safeText = escapeHtml(rawText);
  const cleanKw = (keyword || "").trim();
  if (!cleanKw) return safeText;
  const escapedRegex = escapeHtml(cleanKw).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  try {
    const regex = new RegExp(`(${escapedRegex})`, "gi");
    return safeText.replace(
      regex,
      '<mark class="bg-rose-500/30 text-rose-100 rounded px-0.5">$1</mark>'
    );
  } catch {
    return safeText;
  }
}

export function createSessionsTabController() {
  async function searchSessions() {
    const qInput = document.getElementById("sessions-search-q");
    const uidInput = document.getElementById("sessions-search-uid");
    const roleSelect = document.getElementById("sessions-search-role");
    const tbody = document.getElementById("sessions-search-tbody");
    const countEl = document.getElementById("sessions-result-count");
    if (!tbody) return;

    const q = (qInput?.value || "").trim();
    const userId = (uidInput?.value || "").trim();
    const role = (roleSelect?.value || "").trim();

    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (userId) params.set("user_id", userId);
    if (role) params.set("role", role);
    params.set("limit", "100");

    tbody.innerHTML =
      '<tr><td colspan="3" class="py-6 text-center text-[#8b95a7]">Đang tìm kiếm trên MongoDB Atlas...</td></tr>';

    try {
      const data = await apiRequest(`/api/sessions/search?${params.toString()}`);
      const items = data.items || [];
      if (countEl) {
        countEl.textContent = `${items.length} kết quả`;
      }

      if (!items.length) {
        const emptyMsg = data.db_connected === false
          ? "Chưa kết nối MongoDB Atlas hoặc bộ nhớ phiên đang tắt."
          : "Không tìm thấy tin nhắn nào khớp với điều kiện tìm kiếm.";
        tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center text-[#5e6778]">${escapeHtml(emptyMsg)}</td></tr>`;
        return;
      }

      tbody.innerHTML = items
        .map((item) => {
          const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleString("vi-VN") : "—";
          const isModel = item.role === "model";
          const roleBadge = isModel
            ? `<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-rose-500/15 text-rose-300 border border-rose-500/30">AI${item.model ? ` · ${escapeHtml(item.model)}` : ""}</span>`
            : '<span class="px-1.5 py-0.5 rounded text-[10px] font-medium bg-sky-500/15 text-sky-300 border border-sky-500/30">User</span>';
          const statusLabel = item.session_status === "active"
            ? '<span class="text-[10px] text-emerald-400">● Đang chạy</span>'
            : '<span class="text-[10px] text-[#5e6778]">Lưu trữ</span>';

          return `
            <tr class="hover:bg-[#181d28]/70 align-top">
              <td class="py-3 pr-4">
                <button
                  type="button"
                  data-copy-chat-id="${escapeHtml(item.chat_id)}"
                  title="Bấm để sao chép đầy đủ: ${escapeHtml(item.chat_id)}"
                  class="font-mono text-slate-100 hover:text-rose-400 transition cursor-pointer text-left break-all"
                >${escapeHtml(item.chat_id_short)}</button>
                <div class="text-[10px] text-[#5e6778] font-mono mt-0.5 truncate" title="Discord User ID: ${escapeHtml(item.user_id)}">UID: ${escapeHtml(item.user_id || "—")}</div>
                <div class="mt-0.5">${statusLabel}</div>
              </td>
              <td class="py-3 pr-4">
                <div class="mb-1.5 flex items-center gap-2">${roleBadge}</div>
                <div class="text-slate-200 whitespace-pre-wrap break-words leading-relaxed max-h-48 overflow-y-auto pr-1">${highlightMatch(item.content, q)}</div>
              </td>
              <td class="py-3 text-right font-mono text-[#8b95a7] whitespace-nowrap">${escapeHtml(timeStr)}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center text-rose-400">Lỗi tìm kiếm: ${escapeHtml(err.message)}</td></tr>`;
      showToast(err.message, false);
    }
  }

  function setQueryAndSearch(keyword) {
    const qInput = document.getElementById("sessions-search-q");
    if (qInput) qInput.value = keyword || "";
    searchSessions();
  }

  function bindEvents() {
    const form = document.getElementById("form-search-sessions");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        searchSessions();
      });
    }

    const roleSelect = document.getElementById("sessions-search-role");
    if (roleSelect) {
      roleSelect.addEventListener("change", searchSessions);
    }

    const resetBtn = document.getElementById("btn-reset-sessions-search");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        document.getElementById("sessions-search-q").value = "";
        document.getElementById("sessions-search-uid").value = "";
        document.getElementById("sessions-search-role").value = "";
        searchSessions();
      });
    }

    // Sao chép nhanh mã Chat ID đầy đủ khi bấm vào cột Mã chat
    const tbody = document.getElementById("sessions-search-tbody");
    if (tbody) {
      tbody.addEventListener("click", async (event) => {
        const btn = event.target.closest("button[data-copy-chat-id]");
        if (!btn) return;
        const chatId = btn.dataset.copyChatId;
        try {
          await navigator.clipboard.writeText(chatId);
          showToast(`📋 Đã sao chép mã chat: ${chatId}`);
        } catch {
          showToast(`Mã chat: ${chatId}`);
        }
      });
    }
  }

  return { searchSessions, setQueryAndSearch, bindEvents };
}
