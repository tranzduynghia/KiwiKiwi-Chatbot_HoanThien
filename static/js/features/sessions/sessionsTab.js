/**
 * Bộ điều khiển Tab Tra cứu & duyệt phiên chat:
 * - Tự động tải toàn bộ bản ghi ngay khi mở tab (không bắt buộc nhập ID)
 * - Hỗ trợ lọc theo Vai trò Server, Discord User ID, Username, từ khóa nội dung và loại tin nhắn
 * - Hiển thị Avatar người dùng kèm Mã chat, Nội dung tin nhắn và Thời gian cùng phân trang
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

const PAGE_SIZE = 50;

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

function renderAvatar(item) {
  const display = item.display_name || item.username || item.user_id || "?";
  const initial = escapeHtml(String(display).trim().charAt(0).toUpperCase() || "?");
  if (item.avatar_url) {
    return `
      <img
        src="${escapeHtml(item.avatar_url)}"
        alt="${escapeHtml(display)}"
        loading="lazy"
        class="w-10 h-10 rounded-full object-cover border border-[#2a3140] shrink-0 bg-[#0a0d12]"
      />
    `;
  }
  return `
    <div
      class="w-10 h-10 rounded-full bg-gradient-to-br from-rose-500/80 to-rose-900/90 border border-[#2a3140] text-white font-semibold text-sm flex items-center justify-center shrink-0"
      aria-hidden="true"
    >${initial}</div>
  `;
}

function renderServerRoleBadges(roles) {
  if (!Array.isArray(roles) || !roles.length) return "";
  return `
    <div class="flex flex-wrap gap-1 mt-1">
      ${roles
        .slice(0, 3)
        .map((role) => {
          const color = /^#[0-9a-f]{3,6}$/i.test(role.color || "") ? role.color : "#64748b";
          return `<span class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded bg-[#0a0d12] border border-[#252b38] text-[10px] text-slate-300">
            <span class="w-1.5 h-1.5 rounded-full shrink-0" style="background-color:${color}"></span>
            ${escapeHtml(role.name)}
          </span>`;
        })
        .join("")}
      ${roles.length > 3 ? `<span class="text-[10px] text-[#5e6778]">+${roles.length - 3}</span>` : ""}
    </div>
  `;
}

export function createSessionsTabController() {
  let currentOffset = 0;
  let filtersLoaded = false;

  async function loadServerRoleOptions() {
    const selectEl = document.getElementById("sessions-search-server-role");
    if (!selectEl) return;
    try {
      const data = await apiRequest("/api/sessions/filters");
      const currentVal = selectEl.value;
      const roles = data.server_roles || [];
      selectEl.innerHTML =
        '<option value="">Tất cả vai trò server</option>' +
        roles
          .map((role) => {
            const label = role.guild_name ? `${role.name} (${role.guild_name})` : role.name;
            return `<option value="${escapeHtml(role.id)}">${escapeHtml(label)}</option>`;
          })
          .join("");
      if (currentVal) selectEl.value = currentVal;
      filtersLoaded = true;
    } catch (err) {
      console.error("Không tải được danh sách vai trò server:", err);
    }
  }

  async function searchSessions(offset = 0) {
    if (!filtersLoaded) {
      await loadServerRoleOptions();
    }

    currentOffset = Math.max(0, offset);
    const qInput = document.getElementById("sessions-search-q");
    const uidInput = document.getElementById("sessions-search-uid");
    const usernameInput = document.getElementById("sessions-search-username");
    const serverRoleSelect = document.getElementById("sessions-search-server-role");
    const roleSelect = document.getElementById("sessions-search-role");
    const tbody = document.getElementById("sessions-search-tbody");
    const countEl = document.getElementById("sessions-result-count");
    const pageInfoEl = document.getElementById("sessions-page-info");
    const prevBtn = document.getElementById("btn-sessions-prev");
    const nextBtn = document.getElementById("btn-sessions-next");
    if (!tbody) return;

    const q = (qInput?.value || "").trim();
    const userId = (uidInput?.value || "").trim();
    const username = (usernameInput?.value || "").trim();
    const serverRole = (serverRoleSelect?.value || "").trim();
    const role = (roleSelect?.value || "").trim();

    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (userId) params.set("user_id", userId);
    if (username) params.set("username", username);
    if (serverRole) params.set("server_role", serverRole);
    if (role) params.set("role", role);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(currentOffset));

    tbody.innerHTML =
      '<tr><td colspan="3" class="py-6 text-center text-[#8b95a7]">Đang tải dữ liệu từ MongoDB Atlas...</td></tr>';

    try {
      const data = await apiRequest(`/api/sessions/search?${params.toString()}`);
      const items = data.items || [];
      const total = Number(data.total ?? items.length);
      const hasMore = Boolean(data.has_more);

      if (countEl) {
        countEl.textContent = `${total} kết quả`;
      }
      if (pageInfoEl) {
        const startIdx = total === 0 ? 0 : currentOffset + 1;
        const endIdx = currentOffset + items.length;
        pageInfoEl.textContent = `Hiển thị ${startIdx}–${endIdx} / ${total} bản ghi`;
      }
      if (prevBtn) prevBtn.disabled = currentOffset <= 0;
      if (nextBtn) nextBtn.disabled = !hasMore;

      if (!items.length) {
        const emptyMsg = data.db_connected === false
          ? "Chưa kết nối MongoDB Atlas hoặc bộ nhớ phiên đang tắt."
          : "Không tìm thấy tin nhắn nào khớp với điều kiện lọc.";
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

          const displayName = item.display_name || item.username || "Người dùng Discord";
          const usernameLine = item.username ? `@${item.username}` : "";

          return `
            <tr class="hover:bg-[#181d28]/70 align-top">
              <td class="py-3 pr-4">
                <div class="flex items-start gap-3">
                  ${renderAvatar(item)}
                  <div class="min-w-0 flex-1">
                    <div class="font-medium text-slate-100 truncate">${escapeHtml(displayName)}</div>
                    ${usernameLine ? `<div class="text-[11px] text-[#8b95a7] truncate">${escapeHtml(usernameLine)}</div>` : ""}
                    <div class="flex items-center gap-2 mt-1 flex-wrap">
                      <button
                        type="button"
                        data-copy-chat-id="${escapeHtml(item.chat_id)}"
                        title="Bấm để sao chép đầy đủ: ${escapeHtml(item.chat_id)}"
                        class="font-mono text-[11px] text-rose-300 hover:text-rose-200 bg-[#0a0d12] border border-[#1e232e] px-1.5 py-0.5 rounded transition cursor-pointer"
                      >#${escapeHtml(item.chat_id_short)}</button>
                      ${statusLabel}
                    </div>
                    <div class="text-[10px] text-[#5e6778] font-mono mt-0.5 truncate" title="Discord User ID: ${escapeHtml(item.user_id)}">UID: ${escapeHtml(item.user_id || "—")}</div>
                    ${renderServerRoleBadges(item.server_roles)}
                  </div>
                </div>
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
    searchSessions(0);
  }

  function bindEvents() {
    const form = document.getElementById("form-search-sessions");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        searchSessions(0);
      });
    }

    const serverRoleSelect = document.getElementById("sessions-search-server-role");
    if (serverRoleSelect) {
      serverRoleSelect.addEventListener("change", () => searchSessions(0));
    }

    const roleSelect = document.getElementById("sessions-search-role");
    if (roleSelect) {
      roleSelect.addEventListener("change", () => searchSessions(0));
    }

    const resetBtn = document.getElementById("btn-reset-sessions-search");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        document.getElementById("sessions-search-q").value = "";
        document.getElementById("sessions-search-uid").value = "";
        document.getElementById("sessions-search-username").value = "";
        document.getElementById("sessions-search-server-role").value = "";
        document.getElementById("sessions-search-role").value = "";
        searchSessions(0);
      });
    }

    const prevBtn = document.getElementById("btn-sessions-prev");
    if (prevBtn) {
      prevBtn.addEventListener("click", () => searchSessions(Math.max(0, currentOffset - PAGE_SIZE)));
    }

    const nextBtn = document.getElementById("btn-sessions-next");
    if (nextBtn) {
      nextBtn.addEventListener("click", () => searchSessions(currentOffset + PAGE_SIZE));
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
