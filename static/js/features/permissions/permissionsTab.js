/**
 * Bộ điều khiển Tab 5: Quản lý phân quyền Discord User ID và tài khoản Dashboard.
 * Sử dụng ủy quyền sự kiện (event delegation) tránh gắn hàm toàn cục lên window.
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

export function createPermissionsTabController({ isAdmin }) {
  let discordPermsById = new Map();
  let loginHistCursor = "0";
  let loginHistNext = null;
  let loginHistPrev = null;
  let loginHistStatus = "all";

  async function loadLoginHistory(cursor = "0") {
    const tbody = document.getElementById("login-history-tbody");
    try {
      const tz = detectBrowserTimezone();
      const params = new URLSearchParams({
        limit: "15",
        cursor: String(cursor || "0"),
        status: loginHistStatus,
      });
      if (tz) params.set("tz", tz);
      const data = await apiRequest(`/api/auth/login-history?${params.toString()}`);

      loginHistCursor = data.cursor || "0";
      loginHistNext = data.next_cursor;
      loginHistPrev = data.prev_cursor;

      const tzBadge = document.getElementById("login-history-timezone-badge");
      if (tzBadge && data.timezone_label) {
        tzBadge.textContent = `🕒 Múi giờ IP truy cập: ${data.timezone_label}`;
      }
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
        tbody.innerHTML =
          '<tr><td colspan="5" class="py-6 text-center text-slate-500">Chưa có bản ghi lịch sử đăng nhập nào.</td></tr>';
        return;
      }

      tbody.innerHTML = data.items
        .map((item) => {
          const isSuccess = item.status === "success";
          const statusBadge = isSuccess
            ? '<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/15 text-emerald-300 border border-emerald-500/35">Thành công</span>'
            : `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold bg-rose-500/15 text-rose-300 border border-rose-500/35" title="${escapeHtml(FAILURE_REASON_LABELS[item.failure_reason] || item.failure_reason || "Đăng nhập thất bại")}">Thất bại</span>`;
          const reasonHint =
            !isSuccess && item.failure_reason
              ? `<div class="text-[10px] text-rose-400/80 mt-0.5">${escapeHtml(FAILURE_REASON_LABELS[item.failure_reason] || item.failure_reason)}</div>`
              : "";
          const methodTag =
            item.auth_method === "discord_oauth"
              ? '<span class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/15 text-indigo-300">OAuth2</span>'
              : "";
          const countryPart = item.country_code
            ? ` <span class="ox-badge-neutral px-1.5 py-0.5 rounded text-[10px] font-mono">${escapeHtml(item.country_code)}</span>`
            : "";
          return `
            <tr>
              <td class="py-2.5 pr-3 font-mono text-slate-300 whitespace-nowrap">${escapeHtml(item.created_at_formatted || "—")}</td>
              <td class="py-2.5 pr-3 font-medium text-slate-100">${escapeHtml(item.username || "unknown")}${methodTag}</td>
              <td class="py-2.5 pr-3">${statusBadge}${reasonHint}</td>
              <td class="py-2.5 pr-3 font-mono text-slate-300">${escapeHtml(item.ip_address || "unknown")}${countryPart}</td>
              <td class="py-2.5 text-right text-slate-400">${escapeHtml(item.device_summary || "Không xác định")}</td>
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

  async function loadPermissions() {
    try {
      const [discordData, usersData] = await Promise.all([
        apiRequest("/api/permissions/discord"),
        apiRequest("/api/permissions/users"),
      ]);

      discordPermsById = new Map(discordData.items.map((item) => [String(item.user_id), item]));

      const discordTbody = document.getElementById("discord-perms-tbody");
      discordTbody.innerHTML = discordData.items.length
        ? discordData.items
            .map(
              (item) => `
                <tr data-user-id="${escapeHtml(item.user_id)}">
                  <td class="py-2.5 pr-3 font-mono text-slate-100">${escapeHtml(item.user_id)}</td>
                  <td class="py-2.5 pr-3 text-slate-300">${escapeHtml(item.label || "—")}</td>
                  <td class="py-2.5 pr-3 text-center">
                    <input type="checkbox" data-perm="is_admin" ${item.is_admin ? "checked" : ""} ${isAdmin ? "" : "disabled"}
                      class="accent-indigo-500 w-4 h-4" />
                  </td>
                  <td class="py-2.5 pr-3 text-center">
                    <input type="checkbox" data-perm="can_dashboard" ${item.can_dashboard || item.is_admin ? "checked" : ""} ${isAdmin && !item.is_admin ? "" : "disabled"}
                      class="accent-indigo-500 w-4 h-4" />
                  </td>
                  <td class="py-2.5 pr-3 text-center">
                    <input type="checkbox" data-perm="can_status" ${item.can_status || item.is_admin ? "checked" : ""} ${isAdmin && !item.is_admin ? "" : "disabled"}
                      class="accent-indigo-500 w-4 h-4" />
                  </td>
                  <td class="py-2.5 text-right">
                    ${isAdmin ? '<button type="button" data-action="delete-discord-perm" class="btn-danger">Xóa</button>' : "—"}
                  </td>
                </tr>
              `
            )
            .join("")
        : '<tr><td colspan="6" class="py-3 text-slate-500 text-center">Chưa có Discord User ID nào.</td></tr>';

      const usersTbody = document.getElementById("users-tbody");
      usersTbody.innerHTML = usersData.items.length
        ? usersData.items
            .map(
              (user) => `
                <tr data-username="${escapeHtml(user.username)}">
                  <td class="py-2.5 pr-3 font-medium text-slate-100">${escapeHtml(user.username)}</td>
                  <td class="py-2.5 pr-3">
                    <span class="px-2 py-0.5 rounded text-[11px] ${user.role === "admin" ? "bg-indigo-500/20 text-indigo-300" : "ox-badge-neutral"}">${escapeHtml(user.role)}</span>
                  </td>
                  <td class="py-2.5 pr-3 text-slate-400">
                    ${escapeHtml(user.last_login_at ? new Date(user.last_login_at).toLocaleString("vi-VN") : "Chưa đăng nhập")}
                  </td>
                  <td class="py-2.5 text-right space-x-1.5">
                    ${
                      isAdmin
                        ? '<button type="button" data-action="change-password" class="btn-ghost text-[11px]">Đổi MK</button>' +
                          '<button type="button" data-action="delete-user" class="btn-danger">Xóa</button>'
                        : "—"
                    }
                  </td>
                </tr>
              `
            )
            .join("")
        : '<tr><td colspan="4" class="py-3 text-slate-500 text-center">Chưa có tài khoản mật khẩu nào.</td></tr>';

      await loadLoginHistory("0");
    } catch (err) {
      showToast(err.message, false);
    }
  }

  function bindEvents() {
    const dpAdminEl = document.getElementById("dp-admin");
    if (dpAdminEl) {
      dpAdminEl.addEventListener("change", (event) => {
        if (event.target.checked) {
          document.getElementById("dp-dash").checked = true;
          document.getElementById("dp-status").checked = true;
        }
      });
    }

    // Ủy quyền thay đổi checkbox phân quyền Discord (có xác nhận Before/After, hoàn tác nếu hủy)
    const discordTbody = document.getElementById("discord-perms-tbody");
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
      if (!outcome.confirmed) {
        await loadPermissions();
      }
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
        warning:
          "Hành động này thu hồi ngay lập tức quyền truy cập của Discord User ID khỏi hệ thống và ghi vào Audit Log.",
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

    // Biểu mẫu thêm Discord User ID
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
          : {
              user_id: "",
              label: "",
              is_admin: false,
              can_dashboard: false,
              can_status: false,
            };

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
            document.getElementById("dp-status").checked = true;
            showToast("✅ Đã lưu phân quyền Discord ID!");
            await loadPermissions();
          },
        });
      });
    }

    // Biểu mẫu tạo tài khoản Dashboard mới
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

    // Ủy quyền sự kiện Đổi MK và Xóa trên bảng tài khoản Dashboard
    document.getElementById("users-tbody").addEventListener("click", async (event) => {
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
          changes: [
            {
              key: "username",
              label: "Tài khoản áp dụng",
              before: username,
              after: username,
            },
          ],
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
          changes: [
            {
              key: "username",
              label: "Tài khoản Dashboard bị xóa",
              before: username,
              after: "(Đã xóa khỏi hệ thống)",
              isHighImpact: true,
            },
          ],
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

    // Bộ lọc trạng thái & phân trang cursor cho Lịch sử đăng nhập tài khoản Admin
    const statusFilterEl = document.getElementById("login-history-status-filter");
    if (statusFilterEl) {
      statusFilterEl.addEventListener("change", () => {
        loginHistStatus = statusFilterEl.value || "all";
        loadLoginHistory("0");
      });
    }
    const refreshHistBtn = document.getElementById("btn-refresh-login-history");
    if (refreshHistBtn) {
      refreshHistBtn.addEventListener("click", () => loadLoginHistory(loginHistCursor));
    }
    const prevHistBtn = document.getElementById("btn-login-hist-prev");
    const nextHistBtn = document.getElementById("btn-login-hist-next");
    if (prevHistBtn) {
      prevHistBtn.addEventListener("click", () => {
        if (loginHistPrev !== null && loginHistPrev !== undefined) loadLoginHistory(loginHistPrev);
      });
    }
    if (nextHistBtn) {
      nextHistBtn.addEventListener("click", () => {
        if (loginHistNext !== null && loginHistNext !== undefined) loadLoginHistory(loginHistNext);
      });
    }
  }

  return { loadPermissions, loadLoginHistory, bindEvents };
}
