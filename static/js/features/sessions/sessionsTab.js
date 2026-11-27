/**
 * Bộ điều khiển Tab Tra cứu & duyệt phiên chat:
 * - Zero Prefetch: KHÔNG gọi API khi chưa mở khóa Step-up Auth.
 * - True DOM Destruction: Xóa sạch toàn bộ DOM (bảng, bộ lọc, modal) qua replaceChildren() và xóa trắng state RAM khi khóa.
 * - Tương thích đầy đủ 3 giao diện Normal / Dark / Light (dùng biến ngữ nghĩa ox-*).
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
      class="${sizeClass} rounded-full bg-gradient-to-br from-rose-500/80 to-rose-900/90 border border-[var(--border-strong)] text-white font-semibold text-sm flex items-center justify-center shrink-0"
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
      ${roles.length > maxCount ? `<span class="text-[10px] ox-text-secondary">+${roles.length - maxCount}</span>` : ""}
    </div>
  `;
}

export function createSessionsTabController({ isStepUpUnlocked, onRequestUnlock }) {
  let currentOffset = 0;
  let filtersLoaded = false;
  let searchSeq = 0;
  let lastRenderKey = "";
  let cachedItems = [];
  let cachedServerRoles = [];
  let isMounted = false;
  let escBound = false;

  function renderLockedState() {
    const root = document.getElementById("sessions-content-root");
    if (!root) return;
    root.replaceChildren();

    const card = document.createElement("div");
    card.className = "stepup-lock-card text-center max-w-lg mx-auto space-y-4";

    const iconWrap = document.createElement("div");
    iconWrap.className = "stepup-shield-icon inline-flex items-center justify-center w-12 h-12 mx-auto";
    iconWrap.setAttribute("aria-hidden", "true");
    iconWrap.innerHTML = `
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
      </svg>
    `;

    const title = document.createElement("h3");
    title.className = "text-base font-semibold stepup-text-primary";
    title.textContent = "Mục Tra cứu phiên chat đang được khóa";

    const desc = document.createElement("p");
    desc.className = "text-xs stepup-text-muted leading-relaxed";
    desc.textContent =
      "Lịch sử hội thoại người dùng chỉ được nạp sau khi xác thực lại danh tính (Step-up Auth) và tự động hủy khỏi bộ nhớ & DOM sau 60 giây không thao tác.";

    const unlockBtn = document.createElement("button");
    unlockBtn.type = "button";
    unlockBtn.id = "btn-unlock-sessions";
    unlockBtn.className = "btn-primary stepup-btn-radius text-xs px-4 py-2";
    unlockBtn.textContent = "Xác thực để mở khóa";
    unlockBtn.addEventListener("click", () => {
      if (typeof onRequestUnlock === "function") onRequestUnlock("sessions");
    });

    card.appendChild(iconWrap);
    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(unlockBtn);
    root.appendChild(card);
  }

  function destroyAndLock() {
    currentOffset = 0;
    filtersLoaded = false;
    searchSeq += 1;
    lastRenderKey = "";
    cachedItems = [];
    cachedServerRoles = [];
    isMounted = false;

    const root = document.getElementById("sessions-content-root");
    if (root) root.replaceChildren();
    const modalRoot = document.getElementById("sessions-modal-root");
    if (modalRoot) modalRoot.replaceChildren();
    const bar = document.getElementById("sessions-stepup-bar");
    if (bar) bar.replaceChildren();
    renderLockedState();
  }

  function mountUnlockedDom() {
    const root = document.getElementById("sessions-content-root");
    const modalRoot = document.getElementById("sessions-modal-root");
    if (!root || !modalRoot) return;
    root.replaceChildren();
    modalRoot.replaceChildren();

    root.innerHTML = `
      <form id="form-search-sessions" class="stepup-card p-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-12 gap-3 items-end">
        <div class="lg:col-span-2">
          <label class="block text-[11px] stepup-text-muted mb-1" for="sessions-search-q">Từ khóa nội dung / Mã chat</label>
          <input id="sessions-search-q" type="text" placeholder="Bỏ trống để xem tất cả..." class="stepup-input" />
        </div>
        <div class="lg:col-span-2">
          <label class="block text-[11px] stepup-text-muted mb-1" for="sessions-search-username">Username</label>
          <input id="sessions-search-username" type="text" placeholder="VD: kiwikiwi" class="stepup-input" />
        </div>
        <div class="lg:col-span-2">
          <label class="block text-[11px] stepup-text-muted mb-1" for="sessions-search-uid">Discord User ID</label>
          <input id="sessions-search-uid" type="text" placeholder="VD: 1240503090..." class="stepup-input font-mono" />
        </div>
        <div class="lg:col-span-2">
          <label class="block text-[11px] stepup-text-muted mb-1" for="sessions-search-server-role">Vai trò Server</label>
          <select id="sessions-search-server-role" class="stepup-select">
            <option value="">Tất cả vai trò server</option>
          </select>
        </div>
        <div class="lg:col-span-2">
          <label class="block text-[11px] stepup-text-muted mb-1" for="sessions-search-role">Loại tin</label>
          <select id="sessions-search-role" class="stepup-select">
            <option value="">Tất cả</option>
            <option value="user">User</option>
            <option value="model">Bot AI</option>
          </select>
        </div>
        <div class="lg:col-span-2 flex gap-2">
          <button type="button" id="btn-search-sessions" class="btn-primary stepup-btn-radius flex-1">🔍 Lọc / Xem</button>
          <button type="button" id="btn-reset-sessions-search" class="btn-ghost stepup-btn-radius px-3" title="Xem tất cả (xóa bộ lọc)">↺</button>
        </div>
      </form>

      <div class="stepup-card p-5 space-y-4">
        <div class="flex items-center justify-between gap-2">
          <span class="text-xs font-medium stepup-text-muted">Danh sách phiên chat</span>
          <span id="sessions-result-count" class="stepup-callout text-xs font-mono px-3 py-1">0 kết quả</span>
        </div>
        <div class="stepup-table-wrap">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[var(--border-subtle)] stepup-text-muted">
                <th class="py-2.5 px-3 w-72">Người dùng &amp; Mã phiên (Chat ID)</th>
                <th class="py-2.5 px-3">Xem trước câu hỏi đầu tiên</th>
                <th class="py-2.5 px-3 text-right w-44 whitespace-nowrap">Thời gian phiên</th>
              </tr>
            </thead>
            <tbody id="sessions-search-tbody" class="divide-y divide-[var(--border-subtle)]">
              <tr>
                <td colspan="3" class="py-6 text-center stepup-text-dim">Đang tải danh sách phiên chat từ MongoDB Atlas...</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-[var(--border-subtle)] text-xs stepup-text-muted">
          <div id="sessions-page-info" class="font-mono">Trang 1</div>
          <div class="flex items-center gap-2">
            <button type="button" id="btn-sessions-prev" class="btn-ghost stepup-btn-radius text-xs px-3 py-1.5 disabled:opacity-40" disabled>← Trang trước</button>
            <button type="button" id="btn-sessions-next" class="btn-ghost stepup-btn-radius text-xs px-3 py-1.5 disabled:opacity-40" disabled>Trang sau →</button>
          </div>
        </div>
      </div>
    `;

    modalRoot.innerHTML = `
      <div
        id="session-detail-modal"
        class="ox-modal-backdrop fixed inset-0 z-50 hidden items-center justify-center p-4 bg-slate-950/55 backdrop-blur-[2px] transition-opacity duration-200 ease-out opacity-0 pointer-events-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby="session-detail-title"
      >
        <div class="ox-modal-panel ox-modal-surface stepup-modal-panel w-full max-w-3xl max-h-[86vh] flex flex-col shadow-2xl overflow-hidden border transition-all duration-200 ease-out opacity-0 scale-95 translate-y-2">
          <div class="ox-modal-header p-4 border-b flex items-start justify-between gap-3">
            <div id="session-detail-header" class="flex items-center gap-3 min-w-0">
              <h3 id="session-detail-title" class="text-sm font-semibold ox-text-primary">Chi tiết phiên chat</h3>
            </div>
            <button
              type="button"
              id="btn-close-session-detail"
              class="ox-icon-btn stepup-btn-radius shrink-0"
              title="Đóng cửa sổ"
              aria-label="Đóng"
            >✕</button>
          </div>
          <div id="session-detail-meta" class="ox-modal-meta px-4 py-2.5 border-b flex flex-wrap items-center gap-3 text-[11px]"></div>
          <div id="session-detail-messages" class="ox-modal-body p-4 space-y-3 overflow-y-auto flex-1"></div>
        </div>
      </div>
    `;

    bindMountedEvents();
    isMounted = true;
  }

  async function loadServerRoleOptions() {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    const selectEl = document.getElementById("sessions-search-server-role");
    if (!selectEl) return;
    try {
      const data = await apiRequest("/api/sessions/filters");
      if (!isStepUpUnlocked()) return;
      const currentVal = selectEl.value;
      const roles = data.server_roles || [];
      cachedServerRoles = roles;
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
      if (err && err.code === "STEP_UP_REQUIRED") return;
      console.error("Không tải được danh sách vai trò server:", err);
    }
  }

  function closeSessionDetailModal() {
    const modal = document.getElementById("session-detail-modal");
    if (!modal) return;
    closeModalAnimated(modal);
  }

  async function openSessionDetail(chatId) {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
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
      if (!isStepUpUnlocked()) return;
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
          class="ox-surface-chip font-mono text-rose-400 px-2 py-0.5 stepup-btn-radius cursor-pointer"
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
                  <div class="w-full max-w-[92%] stepup-callout p-3.5 ${
                    isModel ? "ox-chat-bubble-bot" : "ox-chat-bubble-user"
                  }">
                    <div class="flex items-center justify-between gap-2 mb-1.5 text-[11px]">
                      <div class="flex items-center gap-2">
                        ${
                          isModel
                            ? `<span class="px-2 py-0.5 rounded-full bg-rose-500/15 text-rose-400 border border-rose-500/30 font-medium">🤖 Bot AI${msg.model ? ` · ${escapeHtml(msg.model)}` : ""}</span>`
                            : `<span class="px-2 py-0.5 rounded-full bg-sky-500/15 text-sky-400 border border-sky-500/30 font-medium">👤 ${escapeHtml(displayName)}</span>`
                        }
                      </div>
                      <span class="font-mono ox-text-secondary">${escapeHtml(ts)}</span>
                    </div>
                    <div class="text-xs ox-text-primary whitespace-pre-wrap break-words leading-relaxed">${highlightMatch(msg.content, activeKeyword)}</div>
                  </div>
                </div>
              `;
            })
            .join("")
        : '<div class="py-6 text-center text-xs ox-text-secondary">Phiên chat này chưa có tin nhắn nào.</div>';
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") return;
      msgsEl.innerHTML = `<div class="py-6 text-center text-xs text-rose-400">Không tải được chi tiết phiên chat: ${escapeHtml(err.message)}</div>`;
      showToast(err.message, false);
    }
  }

  async function searchSessions(offset = 0) {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    if (!isMounted) mountUnlockedDom();

    const reqSeq = ++searchSeq;
    try {
      if (!filtersLoaded) {
        await loadServerRoleOptions();
      }
      if (reqSeq !== searchSeq || !isStepUpUnlocked()) return;

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
        '<tr><td colspan="3" class="py-6 text-center ox-text-secondary">Đang tải danh sách phiên chat từ MongoDB Atlas...</td></tr>';

      const data = await apiRequest(`/api/sessions/search?${params.toString()}`);
      if (reqSeq !== searchSeq || !isStepUpUnlocked()) return;

      lastRenderKey = JSON.stringify({ q, userId, username, serverRole, role, currentOffset, total: data.total });
      const items = data.items || [];
      cachedItems = items;
      const total = Number(data.total ?? items.length);
      const hasMore = Boolean(data.has_more);

      if (countEl) countEl.textContent = `${total} phiên chat`;
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
        tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center ox-text-secondary">${escapeHtml(emptyMsg)}</td></tr>`;
        return;
      }

      tbody.innerHTML = items
        .map((item) => {
          const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleString("vi-VN") : "—";
          const statusLabel = item.session_status === "active"
            ? '<span class="text-[10px] text-emerald-400">● Đang chạy</span>'
            : '<span class="text-[10px] ox-text-secondary">Lưu trữ</span>';

          const displayName = item.display_name || item.username || "Người dùng Discord";
          const usernameLine = item.username ? `@${item.username}` : "";
          const previewText = item.preview || item.content || "(Phiên chat trống)";
          const turnsCount = Number(item.turns || 0);
          const msgCount = Number(item.message_count || 0);

          return `
            <tr
              data-view-chat-id="${escapeHtml(item.chat_id)}"
              class="ox-table-row align-top cursor-pointer ox-enter"
              title="Bấm để xem toàn bộ hội thoại phiên #${escapeHtml(item.chat_id_short)}"
            >
              <td class="py-3 px-3">
                <div class="flex items-start gap-3">
                  ${renderAvatar(item)}
                  <div class="min-w-0 flex-1">
                    <div class="font-medium ox-text-primary truncate">${escapeHtml(displayName)}</div>
                    ${usernameLine ? `<div class="text-[11px] ox-text-secondary truncate">${escapeHtml(usernameLine)}</div>` : ""}
                    <div class="flex items-center gap-1.5 mt-1 flex-wrap">
                      <span class="ox-surface-chip font-mono text-[11px] text-rose-400 px-1.5 py-0.5 rounded">#${escapeHtml(item.chat_id_short)}</span>
                      <button
                        type="button"
                        data-copy-chat-id="${escapeHtml(item.chat_id)}"
                        title="Sao chép mã Chat ID đầy đủ"
                        class="ox-surface-chip text-[10px] ox-text-secondary px-1 py-0.5 stepup-btn-radius cursor-pointer"
                      >📋</button>
                      ${statusLabel}
                    </div>
                    <div class="text-[10px] ox-text-secondary font-mono mt-0.5 truncate" title="Discord User ID: ${escapeHtml(item.user_id)}">UID: ${escapeHtml(item.user_id || "—")}</div>
                    ${renderServerRoleBadges(item.server_roles)}
                  </div>
                </div>
              </td>
              <td class="py-3 px-3">
                <div class="ox-text-primary leading-relaxed line-clamp-2 break-words">${highlightMatch(previewText, q)}</div>
                <div class="mt-1.5 flex items-center gap-2 text-[10px] ox-text-secondary font-mono">
                  <span class="ox-surface-chip px-1.5 py-0.5 rounded">${turnsCount} lượt</span>
                  <span class="ox-surface-chip px-1.5 py-0.5 rounded">${msgCount} tin nhắn</span>
                </div>
              </td>
              <td class="py-3 px-3 text-right whitespace-nowrap">
                <div class="font-mono ox-text-secondary">${escapeHtml(timeStr)}</div>
                <button
                  type="button"
                  data-view-chat-id="${escapeHtml(item.chat_id)}"
                  class="btn-ghost stepup-btn-radius text-[11px] px-2.5 py-1 mt-2"
                >👁 Xem chi tiết</button>
              </td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") return;
      if (reqSeq !== searchSeq) return;
      const tbody = document.getElementById("sessions-search-tbody");
      if (tbody) {
        tbody.innerHTML = `<tr><td colspan="3" class="py-6 text-center text-rose-400">Lỗi tìm kiếm: ${escapeHtml(err.message)}</td></tr>`;
      }
      showToast(err.message, false);
    }
  }

  function setQueryAndSearch(keyword) {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    if (!isMounted) mountUnlockedDom();
    const qInput = document.getElementById("sessions-search-q");
    if (qInput) qInput.value = keyword || "";
    searchSessions(0);
  }

  function bindMountedEvents() {
    const form = document.getElementById("form-search-sessions");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        event.stopPropagation();
        searchSessions(0);
      });
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
    if (serverRoleSelect) serverRoleSelect.addEventListener("change", () => searchSessions(0));

    const roleSelect = document.getElementById("sessions-search-role");
    if (roleSelect) roleSelect.addEventListener("change", () => searchSessions(0));

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
    if (prevBtn) prevBtn.addEventListener("click", () => searchSessions(Math.max(0, currentOffset - PAGE_SIZE)));

    const nextBtn = document.getElementById("btn-sessions-next");
    if (nextBtn) nextBtn.addEventListener("click", () => searchSessions(currentOffset + PAGE_SIZE));

    const closeBtn = document.getElementById("btn-close-session-detail");
    if (closeBtn) closeBtn.addEventListener("click", closeSessionDetailModal);

    const modal = document.getElementById("session-detail-modal");
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeSessionDetailModal();
      });
    }
  }

  function bindEvents() {
    renderLockedState();

    const sectionEl = document.getElementById("tab-sessions");
    if (sectionEl) {
      sectionEl.addEventListener("click", async (event) => {
        if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
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

    if (!escBound) {
      escBound = true;
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeSessionDetailModal();
      });
    }
  }

  return { searchSessions, setQueryAndSearch, openSessionDetail, destroyAndLock, bindEvents };
}
