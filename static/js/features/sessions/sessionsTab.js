/**
 * Bộ điều khiển Tab Tra cứu & duyệt phiên chat:
 * - Tự động tải toàn bộ bản ghi ngay khi mở tab (không bắt buộc nhập ID)
 * - Hỗ trợ lọc theo Vai trò Server, Discord User ID, Username, từ khóa nội dung và loại tin nhắn
 * - Mở Modal xem chi tiết toàn bộ hội thoại tại chỗ (không tải lại hay chuyển hướng trang)
 */

import { apiRequest, escapeHtml, sanitizeHttpUrl, showToast } from "../../core/api.js";
import { closeModalAnimated, openModalAnimated } from "../../core/modalTransition.js";

const PAGE_SIZE = 50;

function highlightMatch(rawText, keyword) {
  const source = String(rawText ?? "");
  const cleanKw = String(keyword || "").trim();
  if (!cleanKw) return escapeHtml(source);
  const escapedRegex = cleanKw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  try {
    const regex = new RegExp(`(${escapedRegex})`, "gi");
    const parts = source.split(regex);
    return parts
      .map((part, idx) =>
        idx % 2 === 1
          ? `<mark class="bg-rose-500/30 text-rose-100 rounded px-0.5">${escapeHtml(part)}</mark>`
          : escapeHtml(part)
      )
      .join("");
  } catch {
    return escapeHtml(source);
  }
}

function renderAvatar(item, sizeClass = "w-10 h-10") {
  const display = item.display_name || item.username || item.user_id || "?";
  const initial = escapeHtml(String(display).trim().charAt(0).toUpperCase() || "?");
  const safeAvatarUrl = sanitizeHttpUrl(item.avatar_url);
  if (safeAvatarUrl) {
    return `
      <img
        src="${escapeHtml(safeAvatarUrl)}"
        alt="${escapeHtml(display)}"
        loading="lazy"
        class="${sizeClass} ox-surface-chip rounded-full object-cover shrink-0"
      />
    `;
  }
  return `
    <div
      class="${sizeClass} rounded-full bg-gradient-to-br from-rose-500/80 to-rose-900/90 border border-[#2a3140] text-white font-semibold text-sm flex items-center justify-center shrink-0"
      aria-hidden="true"
    >${initial}</div>
  `;
}

function renderServerRoleBadges(roles, maxCount = 3) {
  if (!Array.isArray(roles) || !roles.length) return "";
  return `
    <div class="flex flex-wrap gap-1 mt-1">
      ${roles
        .slice(0, maxCount)
        .map((role) => {
          const color = /^#[0-9a-f]{3,6}$/i.test(role.color || "") ? role.color : "#64748b";
          return `<span class="ox-surface-chip inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px]">
            <span class="w-1.5 h-1.5 rounded-full shrink-0" style="background-color:${color}"></span>
            ${escapeHtml(role.name)}
          </span>`;
        })
        .join("")}
      ${roles.length > maxCount ? `<span class="text-[10px] text-[#5e6778]">+${roles.length - maxCount}</span>` : ""}
    </div>
  `;
}

export function createSessionsTabController() {
  let currentOffset = 0;
  let filtersLoaded = false;
  let searchSeq = 0;
  let lastRenderKey = "";
  let isSilentFetching = false;

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

  function closeSessionDetailModal() {
    const modal = document.getElementById("session-detail-modal");
    if (!modal) return;
    closeModalAnimated(modal);
  }

  async function openSessionDetail(chatId) {
    const modal = document.getElementById("session-detail-modal");
    const headerEl = document.getElementById("session-detail-header");
    const metaEl = document.getElementById("session-detail-meta");
    const msgsEl = document.getElementById("session-detail-messages");
    if (!modal || !headerEl || !metaEl || !msgsEl) return;

    openModalAnimated(modal);
    headerEl.innerHTML = '<h3 id="session-detail-title" class="text-sm font-semibold ox-text-primary">Đang tải chi tiết phiên chat...</h3>';
    metaEl.innerHTML = "";
    msgsEl.innerHTML = '<div class="py-8 text-center text-xs ox-text-secondary">Đang nạp toàn bộ hội thoại từ MongoDB Atlas...</div>';

    try {
      const data = await apiRequest(`/api/sessions/${encodeURIComponent(chatId)}`);
      const sess = data.session;
      const displayName = sess.display_name || sess.username || "Người dùng Discord";
      const usernameLine = sess.username ? `@${sess.username}` : "";

      headerEl.innerHTML = `
        ${renderAvatar(sess, "w-10 h-10")}
        <div class="min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <h3 id="session-detail-title" class="text-sm font-semibold ox-text-primary truncate">${escapeHtml(displayName)}</h3>
            ${usernameLine ? `<span class="text-xs ox-text-secondary">${escapeHtml(usernameLine)}</span>` : ""}
          </div>
          ${renderServerRoleBadges(sess.server_roles, 6)}
        </div>
      `;

      const createdStr = sess.created_at ? new Date(sess.created_at).toLocaleString("vi-VN") : "—";
      const lastStr = sess.last_active ? new Date(sess.last_active).toLocaleString("vi-VN") : "—";
      const statusBadge = sess.session_status === "active"
        ? '<span class="text-emerald-400 font-medium">● Đang chạy</span>'
        : `<span class="ox-text-secondary">Lưu trữ${sess.end_reason ? ` (${escapeHtml(sess.end_reason)})` : ""}</span>`;

      metaEl.innerHTML = `
        <button
          type="button"
          data-copy-chat-id="${escapeHtml(sess.chat_id)}"
          class="ox-surface-chip font-mono text-rose-300 px-2 py-0.5 rounded cursor-pointer"
          title="Bấm để sao chép Chat ID"
        >Chat ID: ${escapeHtml(sess.chat_id)} 📋</button>
        <span class="font-mono">UID: ${escapeHtml(sess.user_id || "—")}</span>
        <span>${statusBadge}</span>
        <span>${Number(sess.turns || 0)} lượt · ${(sess.messages || []).length} tin nhắn</span>
        <span>Bắt đầu: ${escapeHtml(createdStr)}</span>
        <span>Gần nhất: ${escapeHtml(lastStr)}</span>
      `;

      const activeKeyword = (document.getElementById("sessions-search-q")?.value || "").trim();
      const messages = sess.messages || [];
      msgsEl.innerHTML = messages.length
        ? messages
            .map((msg) => {
              const isModel = msg.role === "model";
              const ts = msg.timestamp ? new Date(msg.timestamp).toLocaleString("vi-VN") : "—";
              return `
                <div class="flex ${isModel ? "justify-start" : "justify-end"}">
                  <div class="w-full max-w-[92%] rounded-xl border p-3.5 ${
                    isModel ? "ox-chat-bubble-bot" : "ox-chat-bubble-user"
                  }">
                    <div class="flex items-center justify-between gap-2 mb-1.5 text-[11px]">
                      <div class="flex items-center gap-2">
                        ${
                          isModel
                            ? `<span class="px-2 py-0.5 rounded-full bg-rose-500/15 text-rose-300 border border-rose-500/30 font-medium">🤖 Bot AI${msg.model ? ` · ${escapeHtml(msg.model)}` : ""}</span>`
                            : `<span class="px-2 py-0.5 rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/30 font-medium">👤 ${escapeHtml(displayName)}</span>`
                        }
                      </div>
                      <span class="font-mono text-[#5e6778]">${escapeHtml(ts)}</span>
                    </div>
                    <div class="text-xs text-slate-200 whitespace-pre-wrap break-words leading-relaxed">${highlightMatch(msg.content, activeKeyword)}</div>
                  </div>
                </div>
              `;
            })
            .join("")
        : '<div class="py-6 text-center text-xs text-[#5e6778]">Phiên chat này chưa có tin nhắn nào.</div>';
    } catch (err) {
      msgsEl.innerHTML = `<div class="py-6 text-center text-xs text-rose-400">Không tải được chi tiết phiên chat: ${escapeHtml(err.message)}</div>`;
      showToast(err.message, false);
    }
  }

  async function searchSessions(offset = 0, { silent = false } = {}) {
    if (silent && isSilentFetching) return;
    if (silent) isSilentFetching = true;

    const reqSeq = ++searchSeq;
    try {
      if (!filtersLoaded) {
        await loadServerRoleOptions();
      }
      if (reqSeq !== searchSeq) return;

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

      if (!silent) {
        tbody.innerHTML =
          '<tr><td colspan="3" class="py-6 text-center text-[#8b95a7]">Đang tải danh sách phiên chat từ MongoDB Atlas...</td></tr>';
      }

      const data = await apiRequest(`/api/sessions/search?${params.toString()}`);
      if (reqSeq !== searchSeq) return;

      const nextKey = JSON.stringify({ q, userId, username, serverRole, role, currentOffset, data });
      if (silent && nextKey === lastRenderKey) {
        return;
      }
      lastRenderKey = nextKey;

      const items = data.items || [];
      const total = Number(data.total ?? items.length);
      const hasMore = Boolean(data.has_more);

      if (countEl) {
        countEl.textContent = `${total} phiên chat`;
      }
      if (pageInfoEl) {
        const startIdx = total === 0 ? 0 : currentOffset + 1;
        const endIdx = currentOffset + items.length;
        pageInfoEl.textContent = `Hiển thị ${startIdx}–${endIdx} / ${total} phiên chat`;
      }
      if (prevBtn) prevBtn.disabled = currentOffset <= 0;
      if (nextBtn) nextBtn.disabled = !hasMore;

      if (!items.length) {
        const emptyMsg = data.db_connected === false
          ? "Chưa kết nối MongoDB Atlas hoặc bộ nhớ phiên đang tắt."
          : "Không tìm thấy phiên chat nào khớp với điều kiện lọc.";
        tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center text-[#5e6778]">${escapeHtml(emptyMsg)}</td></tr>`;
        return;
      }

      const rowAnimClass = silent ? "" : "ox-enter";
      tbody.innerHTML = items
        .map((item) => {
          const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleString("vi-VN") : "—";
          const statusLabel = item.session_status === "active"
            ? '<span class="text-[10px] text-emerald-400">● Đang chạy</span>'
            : '<span class="text-[10px] text-[#5e6778]">Lưu trữ</span>';

          const displayName = item.display_name || item.username || "Người dùng Discord";
          const usernameLine = item.username ? `@${item.username}` : "";
          const previewText = item.preview || item.content || "(Phiên chat trống)";
          const turnsCount = Number(item.turns || 0);
          const msgCount = Number(item.message_count || 0);

          return `
            <tr
              data-view-chat-id="${escapeHtml(item.chat_id)}"
              class="ox-table-row align-top cursor-pointer ${rowAnimClass}"
              title="Bấm để xem toàn bộ hội thoại phiên #${escapeHtml(item.chat_id_short)}"
            >
              <td class="py-3 pr-4">
                <div class="flex items-start gap-3">
                  ${renderAvatar(item)}
                  <div class="min-w-0 flex-1">
                    <div class="font-medium ox-text-primary truncate">${escapeHtml(displayName)}</div>
                    ${usernameLine ? `<div class="text-[11px] ox-text-secondary truncate">${escapeHtml(usernameLine)}</div>` : ""}
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                      <span class="ox-surface-chip font-mono text-[11px] text-rose-300 px-1.5 py-0.5 rounded">#${escapeHtml(item.chat_id_short)}</span>
                      <button
                        type="button"
                        data-copy-chat-id="${escapeHtml(item.chat_id)}"
                        title="Sao chép mã Chat ID đầy đủ"
                        class="ox-surface-chip text-[10px] ox-text-secondary px-1 py-0.5 rounded cursor-pointer"
                      >📋</button>
                      ${statusLabel}
                    </div>
                    <div class="text-[10px] ox-text-secondary font-mono mt-0.5 truncate" title="Discord User ID: ${escapeHtml(item.user_id)}">UID: ${escapeHtml(item.user_id || "—")}</div>
                    ${renderServerRoleBadges(item.server_roles)}
                  </div>
                </div>
              </td>
              <td class="py-3 pr-4">
                <div class="ox-text-primary leading-relaxed line-clamp-2 break-words">${highlightMatch(previewText, q)}</div>
                <div class="mt-1.5 flex items-center gap-2 text-[10px] ox-text-secondary font-mono">
                  <span class="ox-surface-chip px-1.5 py-0.5 rounded">${turnsCount} lượt</span>
                  <span class="ox-surface-chip px-1.5 py-0.5 rounded">${msgCount} tin nhắn</span>
                </div>
              </td>
              <td class="py-3 text-right whitespace-nowrap">
                <div class="font-mono ox-text-secondary">${escapeHtml(timeStr)}</div>
                <button
                  type="button"
                  data-view-chat-id="${escapeHtml(item.chat_id)}"
                  class="btn-ghost text-[11px] px-2.5 py-1 mt-2"
                >👁 Xem chi tiết</button>
              </td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (reqSeq !== searchSeq) return;
      if (!silent) {
        tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center text-rose-400">Lỗi tìm kiếm: ${escapeHtml(err.message)}</td></tr>`;
        showToast(err.message, false);
      }
    } finally {
      if (silent) isSilentFetching = false;
    }
  }

  function refreshCurrentSilently() {
    return searchSessions(currentOffset, { silent: true });
  }

  function setQueryAndSearch(keyword) {
    const qInput = document.getElementById("sessions-search-q");
    if (qInput) qInput.value = keyword || "";
    searchSessions(0, { silent: false });
  }

  function bindEvents() {
    const form = document.getElementById("form-search-sessions");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        event.stopPropagation();
        searchSessions(0);
      });
      // Hỗ trợ nhấn Enter trong bất kỳ ô nhập lọc nào mà không làm tải lại trang
      form.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          searchSessions(0);
        }
      });
    }

    const searchBtn = document.getElementById("btn-search-sessions");
    if (searchBtn) {
      searchBtn.addEventListener("click", (event) => {
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

    // Ủy quyền sự kiện mở Modal chi tiết và sao chép Chat ID (không gây chuyển hướng)
    const sectionEl = document.getElementById("tab-sessions");
    if (sectionEl) {
      sectionEl.addEventListener("click", async (event) => {
        const copyBtn = event.target.closest("[data-copy-chat-id]");
        if (copyBtn) {
          event.preventDefault();
          event.stopPropagation();
          const chatId = copyBtn.dataset.copyChatId;
          try {
            await navigator.clipboard.writeText(chatId);
            showToast(`📋 Đã sao chép mã chat: ${chatId}`);
          } catch {
            showToast(`Mã chat: ${chatId}`);
          }
          return;
        }

        const viewEl = event.target.closest("[data-view-chat-id]");
        if (viewEl) {
          event.preventDefault();
          openSessionDetail(viewEl.dataset.viewChatId);
        }
      });
    }

    // Đóng Modal chi tiết bằng nút ×, bấm ra ngoài nền tối hoặc phím Escape
    const closeBtn = document.getElementById("btn-close-session-detail");
    if (closeBtn) {
      closeBtn.addEventListener("click", closeSessionDetailModal);
    }
    const modal = document.getElementById("session-detail-modal");
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeSessionDetailModal();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeSessionDetailModal();
    });
  }

  return { searchSessions, refreshCurrentSilently, setQueryAndSearch, openSessionDetail, bindEvents };
}
