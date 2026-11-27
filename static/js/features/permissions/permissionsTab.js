/**
 * Bộ điều khiển Tab 5: Quản lý phân quyền Discord User ID, tài khoản Dashboard, lịch sử đăng nhập & danh sách đen.
 * - Zero Prefetch: KHÔNG gọi API khi chưa mở khóa Step-up Auth.
 * - True DOM Destruction: Xóa sạch toàn bộ DOM qua replaceChildren() và xóa trắng state RAM khi khóa.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges, showConfirmModal } from "../../core/confirmSave.js";

function detectBrowserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "";
  } catch {
    return "";
  }
}

const FAILURE_REASON_LABELS = {
  invalid_credentials: "Sai tài khoản / mật khẩu",
  rate_limited: "Bị giới hạn tốc độ (429)",
  not_authorized: "Không có quyền Dashboard",
  invalid_oauth_state: "OAuth state hết hạn",
  oauth_exchange_failed: "Lỗi xác thực Discord OAuth",
};

export function createPermissionsTabController({ isAdmin, isStepUpUnlocked, onRequestUnlock }) {
  let discordPermsById = new Map();
  let usersList = [];
  let loginHistCursor = "0";
  let loginHistNext = null;
  let loginHistPrev = null;
  let loginHistStatus = "all";

  let blacklistCursor = "0";
  let blacklistNext = null;
  let blacklistPrev = null;
  let blacklistSearch = "";
  let isMounted = false;

  function renderLockedState() {
    const root = document.getElementById("perms-content-root");
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
    title.textContent = "Mục Phân quyền & Admin đang được khóa";

    const desc = document.createElement("p");
    desc.className = "text-xs stepup-text-muted leading-relaxed";
    desc.textContent =
      "Dữ liệu phân quyền, tài khoản quản trị, lịch sử đăng nhập và danh sách đen chỉ được nạp sau khi xác thực lại danh tính (Step-up Auth) và tự động hủy khỏi DOM sau 60 giây không thao tác.";

    const unlockBtn = document.createElement("button");
    unlockBtn.type = "button";
    unlockBtn.id = "btn-unlock-perms";
    unlockBtn.className = "btn-primary stepup-btn-radius text-xs px-4 py-2";
    unlockBtn.textContent = "Xác thực để mở khóa";
    unlockBtn.addEventListener("click", () => {
      if (typeof onRequestUnlock === "function") onRequestUnlock("perms");
    });

    card.appendChild(iconWrap);
    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(unlockBtn);
    root.appendChild(card);
  }

  function destroyAndLock() {
    discordPermsById = new Map();
    usersList = [];
    loginHistCursor = "0";
    loginHistNext = null;
    loginHistPrev = null;
    loginHistStatus = "all";
    blacklistCursor = "0";
    blacklistNext = null;
    blacklistPrev = null;
    blacklistSearch = "";
    isMounted = false;

    const root = document.getElementById("perms-content-root");
    if (root) root.replaceChildren();
    const bar = document.getElementById("perms-stepup-bar");
    if (bar) bar.replaceChildren();
    renderLockedState();
  }

  function mountUnlockedDom() {
    const root = document.getElementById("perms-content-root");
    if (!root) return;
    root.replaceChildren();

    const operatorNotice = isAdmin
      ? ""
      : `
        <div class="stepup-callout px-3.5 py-3 text-xs">
          ℹ️ Bạn đang đăng nhập với quyền <strong>Operator</strong> (chỉ xem). Chỉ tài khoản <strong>Admin</strong> mới có thể thêm/sửa/xóa quyền.
        </div>
      `;

    const discordFormHtml = isAdmin
      ? `
        <form id="form-add-discord-perm" class="stepup-form-box grid grid-cols-1 sm:grid-cols-12 gap-3 items-end p-3.5">
          <div class="sm:col-span-3">
            <label class="block text-[11px] stepup-text-muted mb-1" for="dp-uid">Discord User ID</label>
            <input id="dp-uid" type="text" required placeholder="1240503090185109536" class="stepup-input font-mono" />
          </div>
          <div class="sm:col-span-3">
            <label class="block text-[11px] stepup-text-muted mb-1" for="dp-label">Tên gợi nhớ</label>
            <input id="dp-label" type="text" placeholder="VD: Chủ bot / Mod" class="stepup-input" />
          </div>
          <div class="sm:col-span-4 flex flex-wrap gap-3 pb-2 text-xs stepup-text-primary">
            <label class="inline-flex items-center gap-1.5 cursor-pointer">
              <input id="dp-admin" type="checkbox" class="accent-rose-500" /> Admin
            </label>
            <label class="inline-flex items-center gap-1.5 cursor-pointer">
              <input id="dp-dash" type="checkbox" class="accent-rose-500" /> Vào Dashboard
            </label>
            <label class="inline-flex items-center gap-1.5 cursor-pointer">
              <input id="dp-status" type="checkbox" checked class="accent-rose-500" /> Dùng /status
            </label>
          </div>
          <div class="sm:col-span-2">
            <button type="submit" class="btn-primary stepup-btn-radius w-full">+ Thêm / Lưu</button>
          </div>
        </form>
      `
      : "";

    const userFormHtml = isAdmin
      ? `
        <form id="form-add-user" class="stepup-form-box grid grid-cols-1 sm:grid-cols-12 gap-3 items-end p-3.5">
          <div class="sm:col-span-3">
            <label class="block text-[11px] stepup-text-muted mb-1" for="nu-username">Tên đăng nhập</label>
            <input id="nu-username" type="text" required placeholder="admin2" class="stepup-input" />
          </div>
          <div class="sm:col-span-4">
            <label class="block text-[11px] stepup-text-muted mb-1" for="nu-password">Mật khẩu (tối thiểu 6 ký tự)</label>
            <input id="nu-password" type="password" required minlength="6" placeholder="••••••••" class="stepup-input" />
          </div>
          <div class="sm:col-span-3">
            <label class="block text-[11px] stepup-text-muted mb-1" for="nu-role">Vai trò</label>
            <select id="nu-role" class="stepup-select">
              <option value="admin">Admin (toàn quyền)</option>
              <option value="operator">Operator (chỉ cấu hình)</option>
            </select>
          </div>
          <div class="sm:col-span-2">
            <button type="submit" class="btn-primary stepup-btn-radius w-full">+ Tạo tài khoản</button>
          </div>
        </form>
      `
      : "";

    const blacklistFormHtml = isAdmin
      ? `
        <form id="form-add-blacklist" class="stepup-form-box grid grid-cols-1 sm:grid-cols-12 gap-3 items-end p-3.5">
          <div class="sm:col-span-3">
            <label for="bl-uid" class="block text-[11px] stepup-text-muted mb-1">Discord User ID (17–20 số)</label>
            <input id="bl-uid" type="text" required pattern="^\\d{17,20}$" placeholder="123456789012345678" class="stepup-input font-mono text-xs" />
          </div>
          <div class="sm:col-span-7">
            <label for="bl-reason" class="block text-[11px] stepup-text-muted mb-1">Lý do cấm sử dụng (bắt buộc, tối đa 300 ký tự)</label>
            <input id="bl-reason" type="text" required maxlength="300" placeholder="VD: Spam lệnh liên tục / vi phạm chính sách cộng đồng" class="stepup-input text-xs" />
          </div>
          <div class="sm:col-span-2">
            <button type="submit" id="btn-submit-blacklist" class="btn-primary stepup-btn-radius w-full whitespace-nowrap text-xs">🚫 Thêm / Cập nhật</button>
          </div>
        </form>
      `
      : "";

    root.innerHTML = `
      ${operatorNotice}

      <div class="stepup-card p-5 space-y-4">
        <h3 class="font-medium text-sm text-rose-400">Phân quyền theo Discord User ID</h3>
        ${discordFormHtml}
        <div class="stepup-table-wrap">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[var(--border-subtle)] stepup-text-muted">
                <th class="py-2.5 px-3">Discord User ID</th>
                <th class="py-2.5 px-3">Ghi chú</th>
                <th class="py-2.5 px-3 text-center">Admin toàn quyền</th>
                <th class="py-2.5 px-3 text-center">Vào Dashboard</th>
                <th class="py-2.5 px-3 text-center">Dùng /status</th>
                <th class="py-2.5 px-3 text-right">Thao tác</th>
              </tr>
            </thead>
            <tbody id="discord-perms-tbody" class="divide-y divide-[var(--border-subtle)]"></tbody>
          </table>
        </div>
      </div>

      <div class="stepup-card p-5 space-y-4">
        <h3 class="font-medium text-sm text-rose-400">Tài khoản đăng nhập Dashboard (Username &amp; Mật khẩu)</h3>
        ${userFormHtml}
        <div class="stepup-table-wrap">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[var(--border-subtle)] stepup-text-muted">
                <th class="py-2.5 px-3">Tên đăng nhập</th>
                <th class="py-2.5 px-3">Vai trò</th>
                <th class="py-2.5 px-3">Đăng nhập gần nhất</th>
                <th class="py-2.5 px-3 text-right">Thao tác</th>
              </tr>
            </thead>
            <tbody id="users-tbody" class="divide-y divide-[var(--border-subtle)]"></tbody>
          </table>
        </div>
      </div>

      <div class="stepup-card p-5 space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 class="font-medium text-sm text-rose-400">Lịch sử đăng nhập tài khoản Admin</h3>
            <p class="text-[11px] stepup-text-muted mt-0.5">
              Ghi nhận cả lượt đăng nhập thành công và thất bại (lưu giữ 90 ngày, không lưu mật khẩu hay token)
            </p>
          </div>
          <span id="login-history-timezone-badge" class="inline-flex items-center gap-1.5 text-[11px] font-mono px-2.5 py-1 rounded-full bg-sky-500/10 text-sky-400 border border-sky-500/30">
            🕒 Múi giờ IP truy cập: Đang xác định...
          </span>
        </div>

        <div class="flex flex-wrap items-center justify-between gap-2.5">
          <div class="flex items-center gap-2">
            <label for="login-history-status-filter" class="text-xs stepup-text-muted">Trạng thái:</label>
            <select id="login-history-status-filter" class="stepup-select text-xs w-auto min-w-[140px]">
              <option value="all">Tất cả</option>
              <option value="success">Thành công</option>
              <option value="failed">Thất bại</option>
            </select>
          </div>
          <div class="flex items-center gap-2">
            <span id="login-history-count" class="text-xs stepup-text-muted font-mono">0 lượt</span>
            <button type="button" id="btn-refresh-login-history" class="btn-ghost stepup-btn-radius text-xs">↻ Làm mới</button>
          </div>
        </div>

        <div class="stepup-table-wrap">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[var(--border-subtle)] stepup-text-muted">
                <th class="py-2.5 px-3">Thời gian (HH:mm:ss DD/MM/YYYY)</th>
                <th class="py-2.5 px-3">Tài khoản</th>
                <th class="py-2.5 px-3">Trạng thái</th>
                <th class="py-2.5 px-3">Địa chỉ IP &amp; Quốc gia</th>
                <th class="py-2.5 px-3 text-right">Thiết bị / Trình duyệt</th>
              </tr>
            </thead>
            <tbody id="login-history-tbody" class="divide-y divide-[var(--border-subtle)]">
              <tr><td colspan="5" class="py-6 text-center stepup-text-dim">Đang tải lịch sử đăng nhập...</td></tr>
            </tbody>
          </table>
        </div>

        <div class="flex items-center justify-between pt-2 border-t border-[var(--border-subtle)] text-xs">
          <span id="login-history-page-info" class="stepup-text-muted font-mono">Trang 1</span>
          <div class="flex items-center gap-2">
            <button type="button" id="btn-login-hist-prev" class="btn-ghost stepup-btn-radius text-xs disabled:opacity-40" disabled>← Trang trước</button>
            <button type="button" id="btn-login-hist-next" class="btn-ghost stepup-btn-radius text-xs disabled:opacity-40" disabled>Trang sau →</button>
          </div>
        </div>
      </div>

      <div class="stepup-card p-5 space-y-4">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 class="font-medium text-sm text-rose-400">🚫 Danh sách đen (Blacklist Discord ID)</h3>
            <p class="text-[11px] stepup-text-muted mt-0.5">
              Chặn ưu tiên cao nhất mọi lệnh <code>/ask</code>, nút bấm, phiên AI và <code>/me</code> kèm hiển thị lý do cấm.
            </p>
          </div>
          <span id="blacklist-timezone-badge" class="inline-flex items-center gap-1.5 text-[11px] font-mono px-2.5 py-1 rounded-full bg-sky-500/10 text-sky-400 border border-sky-500/30">
            🕒 Múi giờ IP truy cập: Đang xác định...
          </span>
        </div>

        ${blacklistFormHtml}

        <div class="flex flex-wrap items-center justify-between gap-2.5">
          <div class="flex items-center gap-2 flex-1 min-w-[220px] max-w-md">
            <input id="blacklist-search" type="text" placeholder="Tìm theo Discord User ID, lý do cấm hoặc người thực hiện..." class="stepup-input text-xs" />
            <button type="button" id="btn-search-blacklist" class="btn-ghost stepup-btn-radius text-xs shrink-0">🔍 Tìm</button>
          </div>
          <div class="flex items-center gap-2">
            <span id="blacklist-count" class="text-xs stepup-text-muted font-mono">0 tài khoản bị chặn</span>
            <button type="button" id="btn-refresh-blacklist" class="btn-ghost stepup-btn-radius text-xs">↻ Làm mới</button>
          </div>
        </div>

        <div class="stepup-table-wrap">
          <table class="w-full text-left text-xs border-collapse">
            <thead>
              <tr class="border-b border-[var(--border-subtle)] stepup-text-muted">
                <th class="py-2.5 px-3 whitespace-nowrap">Discord User ID</th>
                <th class="py-2.5 px-3">Lý do cấm</th>
                <th class="py-2.5 px-3 whitespace-nowrap">Người thực hiện</th>
                <th class="py-2.5 px-3 whitespace-nowrap">Thời gian (HH:mm:ss DD/MM/YYYY)</th>
                <th class="py-2.5 px-3 text-right whitespace-nowrap">Thao tác</th>
              </tr>
            </thead>
            <tbody id="blacklist-tbody" class="divide-y divide-[var(--border-subtle)]">
              <tr><td colspan="5" class="py-6 text-center stepup-text-dim">Đang tải danh sách đen...</td></tr>
            </tbody>
          </table>
        </div>

        <div class="flex items-center justify-between pt-2 border-t border-[var(--border-subtle)] text-xs">
          <span id="blacklist-page-info" class="stepup-text-muted font-mono">Trang 1</span>
          <div class="flex items-center gap-2">
            <button type="button" id="btn-blacklist-prev" class="btn-ghost stepup-btn-radius text-xs disabled:opacity-40" disabled>← Trang trước</button>
            <button type="button" id="btn-blacklist-next" class="btn-ghost stepup-btn-radius text-xs disabled:opacity-40" disabled>Trang sau →</button>
          </div>
        </div>
      </div>
    `;

    bindMountedEvents();
    isMounted = true;
  }

  async function loadBlacklist(cursor = "0") {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    const tbody = document.getElementById("blacklist-tbody");
    try {
      const tz = detectBrowserTimezone();
      const params = new URLSearchParams({ limit: "15", cursor: String(cursor || "0") });
      if (blacklistSearch) params.set("q", blacklistSearch);
      if (tz) params.set("tz", tz);
      const data = await apiRequest(`/api/permissions/blacklist?${params.toString()}`);
      if (!isStepUpUnlocked()) return;

      blacklistCursor = data.cursor || "0";
      blacklistNext = data.next_cursor;
      blacklistPrev = data.prev_cursor;

      const tzBadge = document.getElementById("blacklist-timezone-badge");
      if (tzBadge && data.timezone_label) tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      const countEl = document.getElementById("blacklist-count");
      if (countEl) countEl.textContent = `${data.total || 0} tài khoản bị chặn`;
      const pageNum = Math.floor(Number(blacklistCursor || 0) / (data.limit || 15)) + 1;
      const pageInfo = document.getElementById("blacklist-page-info");
      if (pageInfo) pageInfo.textContent = `Trang ${pageNum} · Hiển thị ${data.items.length}/${data.total || 0}`;

      const prevBtn = document.getElementById("btn-blacklist-prev");
      const nextBtn = document.getElementById("btn-blacklist-next");
      if (prevBtn) prevBtn.disabled = blacklistPrev === null || blacklistPrev === undefined;
      if (nextBtn) nextBtn.disabled = !data.has_more;

      if (!tbody) return;
      if (!data.items.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center ox-text-secondary">Danh sách đen hiện đang trống.</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const actions = isAdmin
            ? `<button type="button" data-action="edit-blacklist" data-uid="${escapeHtml(item.discord_user_id)}" data-reason="${escapeHtml(item.reason)}" class="btn-ghost stepup-btn-radius text-[11px] px-2 py-1">✏️ Sửa lý do</button> ` +
              `<button type="button" data-action="unban-blacklist" data-uid="${escapeHtml(item.discord_user_id)}" data-reason="${escapeHtml(item.reason)}" class="btn-danger stepup-btn-radius text-[11px] px-2 py-1">Gỡ cấm</button>`
            : "—";
          return `
            <tr class="ox-table-row">
              <td class="py-2.5 px-3 font-mono ox-text-primary whitespace-nowrap">${escapeHtml(item.discord_user_id)}</td>
              <td class="py-2.5 px-3 ox-text-primary break-words max-w-md">${escapeHtml(item.reason)}</td>
              <td class="py-2.5 px-3 ox-text-secondary whitespace-nowrap">${escapeHtml(item.banned_by || "admin")}</td>
              <td class="py-2.5 px-3 font-mono ox-text-secondary whitespace-nowrap">${escapeHtml(item.created_at_formatted || "—")}</td>
              <td class="py-2.5 px-3 text-right whitespace-nowrap space-x-1">${actions}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") return;
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
    }
  }

  async function loadLoginHistory(cursor = "0") {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    const tbody = document.getElementById("login-history-tbody");
    try {
      const tz = detectBrowserTimezone();
      const params = new URLSearchParams({ limit: "15", cursor: String(cursor || "0"), status: loginHistStatus });
      if (tz) params.set("tz", tz);
      const data = await apiRequest(`/api/auth/login-history?${params.toString()}`);
      if (!isStepUpUnlocked()) return;

      loginHistCursor = data.cursor || "0";
      loginHistNext = data.next_cursor;
      loginHistPrev = data.prev_cursor;

      const tzBadge = document.getElementById("login-history-timezone-badge");
      if (tzBadge && data.timezone_label) tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      const countEl = document.getElementById("login-history-count");
      if (countEl) countEl.textContent = `${data.total || 0} lượt`;
      const pageNum = Math.floor(Number(loginHistCursor || 0) / (data.limit || 15)) + 1;
      const pageInfo = document.getElementById("login-history-page-info");
      if (pageInfo) pageInfo.textContent = `Trang ${pageNum} · Hiển thị ${data.items.length}/${data.total || 0}`;

      const prevBtn = document.getElementById("btn-login-hist-prev");
      const nextBtn = document.getElementById("btn-login-hist-next");
      if (prevBtn) prevBtn.disabled = loginHistPrev === null || loginHistPrev === undefined;
      if (nextBtn) nextBtn.disabled = !data.has_more;

      if (!tbody) return;
      if (!data.items.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="py-6 text-center ox-text-secondary">Chưa có bản ghi lịch sử đăng nhập nào.</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const isSuccess = item.status === "success";
          const statusBadge = isSuccess
            ? '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 text-emerald-400 border border-emerald-500/35">Thành công</span>'
            : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold bg-rose-500/15 text-rose-400 border border-rose-500/35" title="${escapeHtml(FAILURE_REASON_LABELS[item.failure_reason] || item.failure_reason || "Đăng nhập thất bại")}">Thất bại</span>`;
          const reasonHint = !isSuccess && item.failure_reason
            ? `<div class="text-[10px] text-rose-400/80 mt-0.5">${escapeHtml(FAILURE_REASON_LABELS[item.failure_reason] || item.failure_reason)}</div>`
            : "";
          const methodTag = item.auth_method === "discord_oauth"
            ? '<span class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-400">OAuth2</span>'
            : "";
          const countryPart = item.country_code
            ? ` <span class="ox-badge-neutral px-1.5 py-0.5 rounded text-[10px] font-mono">${escapeHtml(item.country_code)}</span>`
            : "";
          return `
            <tr class="ox-table-row">
              <td class="py-2.5 px-3 font-mono ox-text-secondary whitespace-nowrap">${escapeHtml(item.created_at_formatted || "—")}</td>
              <td class="py-2.5 px-3 font-medium ox-text-primary">${escapeHtml(item.username || "unknown")}${methodTag}</td>
              <td class="py-2.5 px-3">${statusBadge}${reasonHint}</td>
              <td class="py-2.5 px-3 font-mono ox-text-secondary">${escapeHtml(item.ip_address || "unknown")}${countryPart}</td>
              <td class="py-2.5 px-3 text-right ox-text-secondary">${escapeHtml(item.device_summary || "Không xác định")}</td>
            </tr>
          `;
        })
        .join("");
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") return;
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="py-6 text-center text-rose-400">${escapeHtml(err.message)}</td></tr>`;
    }
  }

  async function loadPermissions() {
    if (!isStepUpUnlocked || !isStepUpUnlocked()) return;
    if (!isMounted) mountUnlockedDom();
    try {
      const [discordData, usersData] = await Promise.all([
        apiRequest("/api/permissions/discord"),
        apiRequest("/api/permissions/users"),
      ]);
      if (!isStepUpUnlocked()) return;

      discordPermsById = new Map((discordData.items || []).map((item) => [String(item.user_id), item]));
      usersList = Array.isArray(usersData.items) ? usersData.items : [];

      const discordTbody = document.getElementById("discord-perms-tbody");
      if (discordTbody) {
        discordTbody.innerHTML = discordData.items.length
          ? discordData.items
              .map(
                (item) => `
                  <tr data-user-id="${escapeHtml(item.user_id)}" class="ox-table-row">
                    <td class="py-2.5 px-3 font-mono ox-text-primary">${escapeHtml(item.user_id)}</td>
                    <td class="py-2.5 px-3 ox-text-secondary">${escapeHtml(item.label || "—")}</td>
                    <td class="py-2.5 px-3 text-center">
                      <input type="checkbox" data-perm="is_admin" ${item.is_admin ? "checked" : ""} ${isAdmin ? "" : "disabled"} class="accent-rose-500 w-4 h-4" />
                    </td>
                    <td class="py-2.5 px-3 text-center">
                      <input type="checkbox" data-perm="can_dashboard" ${item.can_dashboard || item.is_admin ? "checked" : ""} ${isAdmin && !item.is_admin ? "" : "disabled"} class="accent-rose-500 w-4 h-4" />
                    </td>
                    <td class="py-2.5 px-3 text-center">
                      <input type="checkbox" data-perm="can_status" ${item.can_status || item.is_admin ? "checked" : ""} ${isAdmin && !item.is_admin ? "" : "disabled"} class="accent-rose-500 w-4 h-4" />
                    </td>
                    <td class="py-2.5 px-3 text-right">
                      ${isAdmin ? '<button type="button" data-action="delete-discord-perm" class="btn-danger stepup-btn-radius">Xóa</button>' : "—"}
                    </td>
                  </tr>
                `
              )
              .join("")
          : '<tr><td colspan="6" class="py-4 ox-text-secondary text-center">Chưa có Discord User ID nào.</td></tr>';
      }

      const usersTbody = document.getElementById("users-tbody");
      if (usersTbody) {
        usersTbody.innerHTML = usersList.length
          ? usersList
              .map(
                (user) => `
                  <tr data-username="${escapeHtml(user.username)}" class="ox-table-row">
                    <td class="py-2.5 px-3 font-medium ox-text-primary">${escapeHtml(user.username)}</td>
                    <td class="py-2.5 px-3">
                      <span class="px-2 py-0.5 rounded text-[11px] ${user.role === "admin" ? "bg-indigo-500/20 text-indigo-400" : "ox-badge-neutral"}">${escapeHtml(user.role)}</span>
                    </td>
                    <td class="py-2.5 px-3 ox-text-secondary">
                      ${escapeHtml(user.last_login_at ? new Date(user.last_login_at).toLocaleString("vi-VN") : "Chưa đăng nhập")}
                    </td>
                    <td class="py-2.5 px-3 text-right space-x-1.5">
                      ${
                        isAdmin
                          ? '<button type="button" data-action="change-password" class="btn-ghost stepup-btn-radius text-[11px]">Đổi MK</button>' +
                            '<button type="button" data-action="delete-user" class="btn-danger stepup-btn-radius">Xóa</button>'
                          : "—"
                      }
                    </td>
                  </tr>
                `
              )
              .join("")
          : '<tr><td colspan="4" class="py-4 ox-text-secondary text-center">Chưa có tài khoản mật khẩu nào.</td></tr>';
      }

      await Promise.all([loadLoginHistory("0"), loadBlacklist("0")]);
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") return;
      showToast(err.message, false);
    }
  }

  function bindMountedEvents() {
    const dpAdminEl = document.getElementById("dp-admin");
    if (dpAdminEl) {
      dpAdminEl.addEventListener("change", (event) => {
        if (event.target.checked) {
          document.getElementById("dp-dash").checked = true;
          document.getElementById("dp-status").checked = true;
        }
      });
    }

    const discordTbody = document.getElementById("discord-perms-tbody");
    if (discordTbody) {
      discordTbody.addEventListener("change", async (event) => {
        const checkbox = event.target.closest("input[data-perm]");
        if (!checkbox) return;
        const row = checkbox.closest("tr[data-user-id]");
        if (!row) return;
        const uid = row.dataset.userId;
        const existing = discordPermsById.get(uid);
        if (!existing) return;

        const beforePerm = {
          is_admin: Boolean(existing.is_admin),
          can_dashboard: Boolean(existing.can_dashboard || existing.is_admin),
          can_status: Boolean(existing.can_status || existing.is_admin),
        };
        const updated = {
          user_id: uid,
          label: existing.label || "",
          is_admin: Boolean(existing.is_admin),
          can_dashboard: Boolean(existing.can_dashboard),
          can_status: Boolean(existing.can_status),
          [checkbox.dataset.perm]: checkbox.checked,
        };
        const afterPerm = {
          is_admin: Boolean(updated.is_admin),
          can_dashboard: Boolean(updated.can_dashboard || updated.is_admin),
          can_status: Boolean(updated.can_status || updated.is_admin),
        };

        const outcome = await confirmSaveChanges({
          title: `Xác nhận đổi quyền Discord ID ${uid}`,
          subtitle: "Thay đổi phân quyền sẽ áp dụng ngay và được lưu vào Audit Log",
          before: beforePerm,
          after: afterPerm,
          onConfirm: async (changes) => {
            await apiRequest("/api/permissions/discord", {
              method: "POST",
              body: JSON.stringify({ ...updated, changes }),
            });
            showToast(`✅ Đã cập nhật quyền cho Discord ID ${uid}.`);
            await loadPermissions();
          },
        });
        if (!outcome.confirmed && isStepUpUnlocked()) await loadPermissions();
      });

      discordTbody.addEventListener("click", async (event) => {
        const btn = event.target.closest('button[data-action="delete-discord-perm"]');
        if (!btn) return;
        const row = btn.closest("tr[data-user-id]");
        if (!row) return;
        const uid = row.dataset.userId;
        const existing = discordPermsById.get(uid);
        await showConfirmModal({
          title: `Xác nhận xóa quyền Discord ID ${uid}`,
          subtitle: "Người dùng này sẽ mất toàn bộ quyền truy cập Dashboard và lệnh quản trị /status",
          warning: "Hành động này thu hồi ngay lập tức quyền truy cập của Discord User ID khỏi hệ thống và ghi vào Audit Log.",
          changes: [
            {
              key: "user_id",
              label: "Discord User ID bị xóa",
              before: existing?.label ? `${uid} (${existing.label})` : uid,
              after: "(Đã thu hồi toàn bộ quyền)",
              isHighImpact: Boolean(existing?.is_admin),
            },
            {
              key: "is_admin",
              label: "Quyền Quản trị viên (Admin)",
              before: Boolean(existing?.is_admin),
              after: false,
              isHighImpact: Boolean(existing?.is_admin),
            },
          ],
          confirmLabel: "Xác nhận xóa",
          confirmTone: "danger",
          onConfirm: async () => {
            await apiRequest(`/api/permissions/discord/${encodeURIComponent(uid)}`, { method: "DELETE" });
            showToast(`Đã xóa Discord ID ${uid}.`);
            await loadPermissions();
          },
        });
      });
    }

    const formAddDiscordPerm = document.getElementById("form-add-discord-perm");
    if (formAddDiscordPerm) {
      formAddDiscordPerm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const uid = document.getElementById("dp-uid").value.trim();
        const existing = discordPermsById.get(uid);
        const payload = {
          user_id: uid,
          label: document.getElementById("dp-label").value.trim(),
          is_admin: document.getElementById("dp-admin").checked,
          can_dashboard: document.getElementById("dp-dash").checked,
          can_status: document.getElementById("dp-status").checked,
        };
        const beforeState = existing
          ? {
              user_id: existing.user_id,
              label: existing.label || "",
              is_admin: Boolean(existing.is_admin),
              can_dashboard: Boolean(existing.can_dashboard),
              can_status: Boolean(existing.can_status),
            }
          : { user_id: "", label: "", is_admin: false, can_dashboard: false, can_status: false };

        await confirmSaveChanges({
          title: existing ? `Xác nhận cập nhật Discord ID ${uid}` : `Xác nhận thêm Discord ID ${uid}`,
          subtitle: "Kiểm tra các quyền được cấp trước khi lưu vào MongoDB Atlas",
          before: beforeState,
          after: payload,
          onConfirm: async (changes) => {
            await apiRequest("/api/permissions/discord", {
              method: "POST",
              body: JSON.stringify({ ...payload, changes }),
            });
            formAddDiscordPerm.reset();
            const dpStatus = document.getElementById("dp-status");
            if (dpStatus) dpStatus.checked = true;
            showToast("✅ Đã lưu phân quyền Discord ID!");
            await loadPermissions();
          },
        });
      });
    }

    const formAddUser = document.getElementById("form-add-user");
    if (formAddUser) {
      formAddUser.addEventListener("submit", async (event) => {
        event.preventDefault();
        const payload = {
          username: document.getElementById("nu-username").value.trim(),
          password: document.getElementById("nu-password").value,
          role: document.getElementById("nu-role").value,
        };
        await confirmSaveChanges({
          title: "Xác nhận tạo tài khoản Dashboard mới",
          subtitle: "Mật khẩu được che an toàn trong bản xem trước và băm scrypt trên server",
          before: { username: "", role: "", password: "" },
          after: payload,
          onConfirm: async (changes) => {
            await apiRequest("/api/permissions/users", {
              method: "POST",
              body: JSON.stringify({ ...payload, changes }),
            });
            formAddUser.reset();
            showToast("✅ Đã tạo tài khoản Dashboard mới!");
            await loadPermissions();
          },
        });
      });
    }

    const usersTbody = document.getElementById("users-tbody");
    if (usersTbody) {
      usersTbody.addEventListener("click", async (event) => {
        const btn = event.target.closest("button[data-action]");
        if (!btn) return;
        const row = btn.closest("tr[data-username]");
        if (!row) return;
        const username = row.dataset.username;

        if (btn.dataset.action === "change-password") {
          await showConfirmModal({
            title: `Đổi mật khẩu cho tài khoản '${username}'`,
            subtitle: "Mật khẩu mới sẽ được băm scrypt trên server và có hiệu lực ở lần đăng nhập kế tiếp",
            inputField: {
              label: `Mật khẩu mới cho '${username}' (tối thiểu 6 ký tự)`,
              placeholder: "Nhập mật khẩu mới...",
              type: "password",
              minLength: 6,
              minLengthMessage: "Mật khẩu mới phải có tối thiểu 6 ký tự.",
              fieldKey: "password",
              beforeValue: "••••• (đã lưu)",
            },
            changes: [{ key: "username", label: "Tài khoản áp dụng", before: username, after: username }],
            confirmLabel: "Xác nhận đổi mật khẩu",
            onConfirm: async (changes, _entries, newPassword) => {
              await apiRequest(`/api/permissions/users/${encodeURIComponent(username)}/password`, {
                method: "PUT",
                body: JSON.stringify({ password: newPassword, changes }),
              });
              showToast(`🔑 Đã đổi mật khẩu cho ${username}.`);
            },
          });
        } else if (btn.dataset.action === "delete-user") {
          await showConfirmModal({
            title: `Xác nhận xóa tài khoản '${username}'`,
            subtitle: "Tài khoản này sẽ không thể đăng nhập Dashboard được nữa",
            warning: `Xóa tài khoản Dashboard '${username}' sẽ có hiệu lực ngay lập tức và được ghi vào Audit Log.`,
            changes: [{ key: "username", label: "Tài khoản Dashboard bị xóa", before: username, after: "(Đã xóa khỏi hệ thống)", isHighImpact: true }],
            confirmLabel: "Xác nhận xóa",
            confirmTone: "danger",
            onConfirm: async () => {
              await apiRequest(`/api/permissions/users/${encodeURIComponent(username)}`, { method: "DELETE" });
              showToast(`Đã xóa tài khoản ${username}.`);
              await loadPermissions();
            },
          });
        }
      });
    }

    const statusFilterEl = document.getElementById("login-history-status-filter");
    if (statusFilterEl) {
      statusFilterEl.addEventListener("change", () => {
        loginHistStatus = statusFilterEl.value || "all";
        loadLoginHistory("0");
      });
    }
    const refreshHistBtn = document.getElementById("btn-refresh-login-history");
    if (refreshHistBtn) refreshHistBtn.addEventListener("click", () => loadLoginHistory(loginHistCursor));
    const prevHistBtn = document.getElementById("btn-login-hist-prev");
    const nextHistBtn = document.getElementById("btn-login-hist-next");
    if (prevHistBtn) prevHistBtn.addEventListener("click", () => { if (loginHistPrev !== null && loginHistPrev !== undefined) loadLoginHistory(loginHistPrev); });
    if (nextHistBtn) nextHistBtn.addEventListener("click", () => { if (loginHistNext !== null && loginHistNext !== undefined) loadLoginHistory(loginHistNext); });

    const blForm = document.getElementById("form-add-blacklist");
    if (blForm) {
      blForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const uid = (document.getElementById("bl-uid")?.value || "").trim();
        const reason = (document.getElementById("bl-reason")?.value || "").trim();
        if (!/^\d{17,20}$/.test(uid)) {
          showToast("Discord User ID không hợp lệ (phải gồm 17–20 chữ số).", false);
          return;
        }
        if (!reason) {
          showToast("Vui lòng nhập lý do cấm sử dụng.", false);
          return;
        }
        await showConfirmModal({
          title: `Xác nhận thêm Discord ID ${uid} vào danh sách đen`,
          subtitle: "Người dùng sẽ bị chặn tức thì trên mọi lệnh /ask, nút bấm, phiên AI và /me",
          warning: `Tài khoản ${uid} sẽ nhận thông báo bị hạn chế kèm lý do: "${reason}"`,
          changes: [{ key: `discord_blacklist.${uid}.reason`, label: `Lý do cấm (${uid})`, before: "(Chưa bị cấm)", after: reason, isHighImpact: true }],
          confirmLabel: "Xác nhận cấm",
          confirmTone: "danger",
          onConfirm: async (changes) => {
            await apiRequest("/api/permissions/blacklist", {
              method: "POST",
              body: JSON.stringify({ discord_user_id: uid, reason, changes }),
            });
            blForm.reset();
            showToast(`🚫 Đã thêm Discord ID ${uid} vào danh sách đen.`);
            await loadBlacklist("0");
          },
        });
      });
    }

    const blTbody = document.getElementById("blacklist-tbody");
    if (blTbody) {
      blTbody.addEventListener("click", async (event) => {
        const btn = event.target.closest("button[data-action]");
        if (!btn) return;
        const uid = btn.dataset.uid || "";
        const prevReason = btn.dataset.reason || "";
        if (!uid) return;

        if (btn.dataset.action === "edit-blacklist") {
          await showConfirmModal({
            title: `Sửa lý do cấm cho Discord ID ${uid}`,
            subtitle: "Lý do mới sẽ hiển thị ngay cho người dùng khi bị chặn và được ghi Audit Log",
            inputField: {
              label: "Lý do cấm mới (tối đa 300 ký tự)",
              type: "text",
              placeholder: "Nhập lý do cấm mới...",
              initialValue: prevReason,
              minLength: 1,
              minLengthMessage: "Vui lòng nhập lý do cấm sử dụng.",
              fieldKey: `discord_blacklist.${uid}.reason`,
              beforeValue: prevReason,
            },
            changes: [{ key: "discord_user_id", label: "Discord User ID", before: uid, after: uid }],
            confirmLabel: "Cập nhật lý do",
            onConfirm: async (changes, _entries, newReason) => {
              await apiRequest("/api/permissions/blacklist", {
                method: "POST",
                body: JSON.stringify({ discord_user_id: uid, reason: newReason, changes }),
              });
              showToast(`✏️ Đã cập nhật lý do cấm cho ${uid}.`);
              await loadBlacklist(blacklistCursor);
            },
          });
        } else if (btn.dataset.action === "unban-blacklist") {
          await showConfirmModal({
            title: `Xác nhận gỡ cấm Discord ID ${uid}`,
            subtitle: "Tài khoản này sẽ được mở khóa tức thì và có thể sử dụng lại KiwiKiwi Bot",
            warning: `Gỡ cấm ${uid} khỏi danh sách đen sẽ có hiệu lực ngay lập tức và được ghi vào Audit Log.`,
            changes: [{ key: `discord_blacklist.${uid}.reason`, label: `Trạng thái cấm (${uid})`, before: prevReason || "(Đang bị cấm)", after: "(Đã gỡ cấm)" }],
            confirmLabel: "Xác nhận gỡ cấm",
            confirmTone: "success",
            onConfirm: async () => {
              await apiRequest(`/api/permissions/blacklist/${encodeURIComponent(uid)}`, { method: "DELETE" });
              showToast(`✅ Đã gỡ cấm Discord ID ${uid}.`);
              await loadBlacklist(blacklistCursor);
            },
          });
        }
      });
    }

    const blSearchInput = document.getElementById("blacklist-search");
    const blSearchBtn = document.getElementById("btn-search-blacklist");
    const blRefreshBtn = document.getElementById("btn-refresh-blacklist");
    const triggerBlSearch = () => {
      blacklistSearch = blSearchInput ? blSearchInput.value.trim() : "";
      loadBlacklist("0");
    };
    if (blSearchBtn) blSearchBtn.addEventListener("click", triggerBlSearch);
    if (blSearchInput) {
      blSearchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          triggerBlSearch();
        }
      });
    }
    if (blRefreshBtn) blRefreshBtn.addEventListener("click", () => loadBlacklist(blacklistCursor));
    const blPrevBtn = document.getElementById("btn-blacklist-prev");
    const blNextBtn = document.getElementById("btn-blacklist-next");
    if (blPrevBtn) blPrevBtn.addEventListener("click", () => { if (blacklistPrev !== null && blacklistPrev !== undefined) loadBlacklist(blacklistPrev); });
    if (blNextBtn) blNextBtn.addEventListener("click", () => { if (blacklistNext !== null && blacklistNext !== undefined) loadBlacklist(blacklistNext); });
  }

  function bindEvents() {
    renderLockedState();
  }

  return { loadPermissions, loadLoginHistory, loadBlacklist, destroyAndLock, bindEvents };
}
