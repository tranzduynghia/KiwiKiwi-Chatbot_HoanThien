/**
 * Bộ điều khiển Tab 5: Quản lý phân quyền Discord User ID và tài khoản Dashboard.
 * Sử dụng ủy quyền sự kiện (event delegation) tránh gắn hàm toàn cục lên window.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";

export function createPermissionsTabController({ isAdmin }) {
  let discordPermsById = new Map();

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
                    <span class="px-2 py-0.5 rounded text-[11px] ${user.role === "admin" ? "bg-indigo-500/20 text-indigo-300" : "bg-slate-800 text-slate-400"}">${escapeHtml(user.role)}</span>
                  </td>
                  <td class="py-2.5 pr-3 text-slate-400">
                    ${user.last_login_at ? new Date(user.last_login_at).toLocaleString("vi-VN") : "Chưa đăng nhập"}
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
      if (!confirm(`Xóa quyền của Discord User ID ${uid}?`)) return;
      try {
        await apiRequest(`/api/permissions/discord/${encodeURIComponent(uid)}`, { method: "DELETE" });
        showToast(`Đã xóa Discord ID ${uid}.`);
        await loadPermissions();
      } catch (err) {
        showToast(err.message, false);
      }
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
        const newPassword = prompt(`Nhập mật khẩu mới cho '${username}' (tối thiểu 6 ký tự):`);
        if (!newPassword) return;
        await confirmSaveChanges({
          title: `Xác nhận đổi mật khẩu cho '${username}'`,
          subtitle: "Mật khẩu mới sẽ có hiệu lực ngay ở lần đăng nhập kế tiếp",
          before: { password: "••••• (đã lưu)" },
          after: { password: newPassword },
          onConfirm: async (changes) => {
            await apiRequest(`/api/permissions/users/${encodeURIComponent(username)}/password`, {
              method: "PUT",
              body: JSON.stringify({ password: newPassword, changes }),
            });
            showToast(`🔑 Đã đổi mật khẩu cho ${username}.`);
          },
        });
      } else if (btn.dataset.action === "delete-user") {
        if (!confirm(`Xóa tài khoản '${username}'?`)) return;
        try {
          await apiRequest(`/api/permissions/users/${encodeURIComponent(username)}`, { method: "DELETE" });
          showToast(`Đã xóa tài khoản ${username}.`);
          await loadPermissions();
        } catch (err) {
          showToast(err.message, false);
        }
      }
    });
  }

  return { loadPermissions, bindEvents };
}
