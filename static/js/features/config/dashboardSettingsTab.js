/**
 * Bộ điều khiển Tab "Cấu hình Dashboard" (Bảo mật Step-up Auth, Cooldown & Chu kỳ Discord Presence).
 * - Zero Prefetch: KHÔNG gọi GET /api/dashboard-settings khi chưa mở khóa Step-up Auth.
 * - True DOM Destruction: Xóa sạch DOM bằng replaceChildren() và xóa trắng state RAM khi khóa.
 */

import { apiRequest, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";

const LOCKABLE_SECTION_ITEMS = [
  { key: "perms", label: "Phân quyền & Admin", desc: "Quản lý Discord User ID, tài khoản Dashboard & Danh sách đen" },
  { key: "sessions", label: "Tra cứu phiên chat", desc: "Lịch sử hội thoại và bộ lọc theo vai trò / UID" },
  { key: "temp", label: "Trang nháp (/temp)", desc: "Trang soạn thảo nháp nội bộ ẩn khỏi công chúng" },
  { key: "legal", label: "Điều khoản & Pháp lý", desc: "Trình soạn thảo Rich-Text 5 trang & danh sách chấp thuận ToS" },
  { key: "audit", label: "Nhật ký chỉnh sửa", desc: "Lịch sử thao tác cấu hình & thay đổi hệ thống" },
  { key: "ask", label: "Cấu hình /ask", desc: "Hạn mức, thinking budget và tùy chọn lệnh /ask" },
  { key: "prompt", label: "System Instruction", desc: "Prompt hệ thống và OCR Instruction" },
  { key: "models", label: "Model Gemini", desc: "Chuỗi model tự động và giới hạn RPM/RPD" },
  { key: "status", label: "Trạng thái Bot", desc: "Giám sát tài nguyên, Tunnel và Discord Presence" },
];

export function createDashboardSettingsTabController({
  isAdmin,
  isStepUpUnlocked,
  onRequestUnlock,
  onSettingsUpdated,
}) {
  let initialSnapshot = null;

  function renderLockedState() {
    const root = document.getElementById("dashboard-settings-content-root");
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
    title.textContent = "Mục Cấu hình Dashboard đang được khóa";

    const desc = document.createElement("p");
    desc.className = "text-xs stepup-text-muted leading-relaxed";
    desc.textContent =
      "Để bảo vệ thiết lập khóa Step-up Auth và chu kỳ Presence, vui lòng xác thực lại danh tính. Phiên tự khóa sau thời gian nhàn rỗi.";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-primary stepup-btn-radius text-xs";
    btn.textContent = "🔓 Xác thực để mở khóa";
    btn.addEventListener("click", () => {
      if (typeof onRequestUnlock === "function") {
        onRequestUnlock("dashboard-settings");
      }
    });

    card.appendChild(iconWrap);
    card.appendChild(title);
    card.appendChild(desc);
    card.appendChild(btn);
    root.appendChild(card);
  }

  function destroyAndLock() {
    initialSnapshot = null;
    const bar = document.getElementById("dashboard-settings-stepup-bar");
    if (bar) bar.replaceChildren();
    renderLockedState();
  }

  function renderSettingsUi(data) {
    const root = document.getElementById("dashboard-settings-content-root");
    if (!root) return;
    root.replaceChildren();

    const lockedSet = new Set(Array.isArray(data.locked_sections) ? data.locked_sections : ["perms", "sessions", "temp"]);
    const cooldownSec = Number(data.step_up_cooldown_sec) || 60;
    const intervalSec = Number(data.presence_interval_sec) || 20;
    const autoRotate = Boolean(data.presence_auto_rotate);
    const disabledAttr = isAdmin ? "" : "disabled";

    initialSnapshot = {
      "security.locked_sections": Array.from(lockedSet).sort().join(", "),
      "security.step_up_cooldown_sec": cooldownSec,
      "security.presence_interval_sec": intervalSec,
      "presence.auto_rotate": autoRotate,
    };

    const switchesHtml = LOCKABLE_SECTION_ITEMS.map((item) => {
      const checked = lockedSet.has(item.key) ? "checked" : "";
      return `
        <label class="flex items-start justify-between gap-3 p-3 rounded-[10px] border border-[var(--border-subtle)] bg-[var(--bg-base)] cursor-pointer hover:border-[var(--border-strong)] transition-colors">
          <div class="min-w-0">
            <div class="text-xs font-semibold stepup-text-primary">${item.label}</div>
            <div class="text-[11px] stepup-text-muted mt-0.5">${item.desc}</div>
          </div>
          <input type="checkbox" class="ds-lock-toggle w-4 h-4 accent-rose-500 shrink-0 mt-0.5" data-section-key="${item.key}" ${checked} ${disabledAttr} />
        </label>
      `;
    }).join("");

    root.innerHTML = `
      <div class="card p-5 space-y-4">
        <div class="border-b border-[var(--border-subtle)] pb-3">
          <h3 class="font-semibold text-sm text-rose-400">1. Quản lý khóa bảo mật từng mục (Step-Up Auth)</h3>
          <p class="text-[11px] stepup-text-muted mt-0.5">
            Chọn các mục bắt buộc xác thực lại khi mở trên Dashboard. Riêng mục <strong>Cấu hình Dashboard</strong> luôn được khóa cứng để chống tự gỡ bảo mật.
          </p>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          ${switchesHtml}
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-5">
        <div class="card p-5 space-y-3">
          <div class="border-b border-[var(--border-subtle)] pb-2.5">
            <h3 class="font-semibold text-sm text-rose-400">2. Thời gian Cooldown / Idle Timeout</h3>
            <p class="text-[11px] stepup-text-muted mt-0.5">
              Tự động khóa lại các mục nhạy cảm khi không thao tác (15–300 giây, mặc định 60 giây). Luôn tính từ lúc xác thực thành công.
            </p>
          </div>
          <div>
            <label for="ds-stepup-cooldown" class="block text-xs stepup-text-muted mb-1">Số giây tự khóa nhàn rỗi (15–300s)</label>
            <input id="ds-stepup-cooldown" type="number" min="15" max="300" step="1" value="${cooldownSec}" class="inp font-mono" ${disabledAttr} />
          </div>
        </div>

        <div class="card p-5 space-y-3">
          <div class="border-b border-[var(--border-subtle)] pb-2.5">
            <h3 class="font-semibold text-sm text-rose-400">3. Chu kỳ luân phiên Discord Presence</h3>
            <p class="text-[11px] stepup-text-muted mt-0.5">
              Điều chỉnh khoảng thời gian luân phiên giữa Custom Status và Hoạt động chính (15–3600 giây), áp dụng ngay không cần restart.
            </p>
          </div>
          <div class="space-y-3">
            <div>
              <label for="ds-presence-interval" class="block text-xs stepup-text-muted mb-1">Chu kỳ luân phiên (15–3600 giây)</label>
              <input id="ds-presence-interval" type="number" min="15" max="3600" step="1" value="${intervalSec}" class="inp font-mono" ${disabledAttr} />
            </div>
            <label class="flex items-center justify-between gap-3 p-2.5 rounded-[10px] border border-[var(--border-subtle)] bg-[var(--bg-base)] cursor-pointer">
              <span class="text-xs stepup-text-primary">Bật luân phiên tự động (Auto-Rotate Presence)</span>
              <input id="ds-presence-auto-rotate" type="checkbox" class="w-4 h-4 accent-rose-500" ${autoRotate ? "checked" : ""} ${disabledAttr} />
            </label>
          </div>
        </div>
      </div>

      ${
        isAdmin
          ? `<div class="flex items-center justify-end gap-2">
              <button type="button" id="btn-reload-dashboard-settings" class="btn-ghost text-xs">↻ Tải lại</button>
              <button type="button" id="btn-save-dashboard-settings" class="btn-primary text-xs">💾 Lưu &amp; Áp dụng tức thì</button>
            </div>`
          : ""
      }
    `;

    const reloadBtn = document.getElementById("btn-reload-dashboard-settings");
    if (reloadBtn) reloadBtn.addEventListener("click", () => loadSettings());

    const saveBtn = document.getElementById("btn-save-dashboard-settings");
    if (saveBtn && isAdmin) {
      saveBtn.addEventListener("click", async () => {
        const selectedSections = Array.from(root.querySelectorAll(".ds-lock-toggle:checked"))
          .map((el) => el.dataset.sectionKey)
          .filter(Boolean);
        const nextCooldown = Math.max(15, Math.min(300, Number(document.getElementById("ds-stepup-cooldown")?.value) || 60));
        const nextInterval = Math.max(15, Math.min(3600, Number(document.getElementById("ds-presence-interval")?.value) || 20));
        const nextAutoRotate = Boolean(document.getElementById("ds-presence-auto-rotate")?.checked);

        const nextSnapshot = {
          "security.locked_sections": [...selectedSections].sort().join(", "),
          "security.step_up_cooldown_sec": nextCooldown,
          "security.presence_interval_sec": nextInterval,
          "presence.auto_rotate": nextAutoRotate,
        };

        await confirmSaveChanges({
          title: "Xác nhận lưu Cấu hình Dashboard & Bảo mật",
          subtitle: "Áp dụng ngay lập tức cho bộ đếm Step-up Auth và vòng lặp Discord Presence đang chạy",
          before: initialSnapshot || {},
          after: nextSnapshot,
          confirmLabel: "Lưu & Áp dụng ngay",
          confirmTone: "primary",
          onConfirm: async (changes) => {
            const res = await apiRequest("/api/dashboard-settings", {
              method: "PUT",
              cache: "no-store",
              body: JSON.stringify({
                locked_sections: selectedSections,
                step_up_cooldown_sec: nextCooldown,
                presence_interval_sec: nextInterval,
                presence_auto_rotate: nextAutoRotate,
                changes,
              }),
            });
            if (typeof onSettingsUpdated === "function") {
              onSettingsUpdated(res);
            }
            showToast("✅ Đã lưu và áp dụng tức thì Cấu hình Dashboard!", true);
            await loadSettings();
          },
        });
      });
    }
  }

  async function loadSettings() {
    if (!isStepUpUnlocked()) {
      destroyAndLock();
      return;
    }
    try {
      const data = await apiRequest("/api/dashboard-settings", { cache: "no-store" });
      renderSettingsUi(data);
    } catch (err) {
      if (err && err.code === "STEP_UP_REQUIRED") {
        destroyAndLock();
        return;
      }
      showToast("Không tải được Cấu hình Dashboard: " + err.message, false);
    }
  }

  renderLockedState();
  return { loadSettings, destroyAndLock };
}
