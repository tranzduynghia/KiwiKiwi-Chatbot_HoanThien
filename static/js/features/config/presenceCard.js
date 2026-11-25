/**
 * Bộ điều khiển thẻ "Bot Presence & Status":
 * - Chọn trạng thái trực tuyến (online / idle / dnd / invisible) kèm chấm màu
 * - Tùy chỉnh song song Custom Status (bong bóng chữ cạnh avatar) VÀ Hoạt động chính (None / Playing / Listening to / Watching / Competing in)
 * - Xem trước trực tiếp giao diện hồ sơ Discord và gọi PUT /api/config/presence để áp dụng tức thì
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

const STATUS_META = {
  online: { label: "Trực tuyến (online)", dotClass: "bg-emerald-500" },
  idle: { label: "Chờ (idle)", dotClass: "bg-amber-400" },
  dnd: { label: "Không làm phiền (dnd)", dotClass: "bg-rose-500" },
  invisible: { label: "Ẩn (invisible)", dotClass: "bg-slate-500" },
};

const ACTIVITY_PREFIX = {
  playing: "Playing",
  listening: "Listening to",
  watching: "Watching",
  competing: "Competing in",
  none: "",
};

export function createPresenceCardController({ onConfigSaved }) {
  let selectedStatus = "online";

  function updateStatusButtons() {
    document.querySelectorAll(".presence-status-btn").forEach((btn) => {
      btn.dataset.selected = String(btn.dataset.presenceStatus === selectedStatus);
    });
  }

  function renderPreview() {
    const customEl = document.getElementById("presence-custom-status");
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    const dotEl = document.getElementById("presence-preview-dot");
    const customWrapEl = document.getElementById("presence-preview-custom-wrap");
    const customTextEl = document.getElementById("presence-preview-custom-text");
    const activityEl = document.getElementById("presence-preview-activity");
    const statusLabelEl = document.getElementById("presence-preview-status-label");
    if (!typeEl || !textEl || !dotEl || !activityEl || !statusLabelEl) return;

    const customVal = customEl ? customEl.value.trim() : "";
    const activityType = typeEl.value || "none";
    const textVal = textEl.value.trim();
    textEl.disabled = activityType === "none";

    const meta = STATUS_META[selectedStatus] || STATUS_META.online;
    dotEl.className = `w-3.5 h-3.5 rounded-full ${meta.dotClass} border-2 border-[#0a0d12] absolute -bottom-0.5 -right-0.5 transition-colors duration-200`;
    statusLabelEl.textContent = meta.label;

    // Hiển thị hoặc ẩn bong bóng Custom Status cạnh avatar
    if (customWrapEl && customTextEl) {
      if (customVal) {
        customWrapEl.classList.remove("hidden");
        customTextEl.textContent = customVal;
      } else {
        customWrapEl.classList.add("hidden");
        customTextEl.textContent = "";
      }
    }

    // Hiển thị dòng Hoạt động chính (có thể song song cùng bong bóng Custom Status)
    const hasPrimaryActivity = activityType !== "none" && Boolean(textVal);
    if (hasPrimaryActivity) {
      const prefix = ACTIVITY_PREFIX[activityType] || "Listening to";
      activityEl.classList.remove("hidden");
      activityEl.innerHTML = `${escapeHtml(prefix)} <strong class="text-slate-200 font-medium">${escapeHtml(textVal)}</strong>`;
    } else if (customVal) {
      activityEl.classList.add("hidden");
      activityEl.innerHTML = "";
    } else {
      activityEl.classList.remove("hidden");
      activityEl.innerHTML = '<span class="text-[#5e6778] italic">Không hiển thị hoạt động</span>';
    }
  }

  function fill(config) {
    const presence = config?.presence || {};
    selectedStatus = STATUS_META[presence.status] ? presence.status : "online";

    const customEl = document.getElementById("presence-custom-status");
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");

    let customVal = presence.custom_status ?? "";
    let activityType = presence.activity_type || "listening";
    let activityText = presence.activity_text ?? "m!help";

    // Tương thích ngược với cấu hình cũ từng lưu activity_type === "custom"
    if (activityType === "custom") {
      if (!customVal && activityText) {
        customVal = activityText;
      }
      activityType = "none";
      activityText = "";
    }

    if (!Object.prototype.hasOwnProperty.call(ACTIVITY_PREFIX, activityType)) {
      activityType = "none";
    }

    if (customEl) customEl.value = customVal;
    if (typeEl) typeEl.value = activityType;
    if (textEl) textEl.value = activityText;

    updateStatusButtons();
    renderPreview();
  }

  function syncBotName(username) {
    const nameEl = document.getElementById("presence-preview-botname");
    if (nameEl && username && username !== "—") {
      nameEl.textContent = username;
    }
  }

  function bindEvents() {
    const groupEl = document.getElementById("presence-status-group");
    if (groupEl) {
      groupEl.addEventListener("click", (event) => {
        const btn = event.target.closest("button[data-presence-status]");
        if (!btn) return;
        selectedStatus = btn.dataset.presenceStatus;
        updateStatusButtons();
        renderPreview();
      });
    }

    const customEl = document.getElementById("presence-custom-status");
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    if (customEl) customEl.addEventListener("input", renderPreview);
    if (typeEl) typeEl.addEventListener("change", renderPreview);
    if (textEl) textEl.addEventListener("input", renderPreview);

    const saveBtn = document.getElementById("btn-save-presence");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const customStatus = customEl ? customEl.value.trim() : "";
        const activityType = typeEl ? typeEl.value : "none";
        const activityText = textEl ? textEl.value.trim() : "";
        saveBtn.disabled = true;
        try {
          const res = await apiRequest("/api/config/presence", {
            method: "PUT",
            body: JSON.stringify({
              status: selectedStatus,
              custom_status: customStatus,
              activity_type: activityType,
              activity_text: activityText,
            }),
          });
          showToast(
            res.applied_live
              ? "✅ Đã cập nhật trạng thái Discord tức thì và lưu MongoDB Atlas!"
              : "✅ Đã lưu cấu hình trạng thái vào MongoDB Atlas (sẽ tự áp dụng khi bot Online)."
          );
          if (onConfigSaved) await onConfigSaved();
        } catch (err) {
          showToast(err.message, false);
        } finally {
          saveBtn.disabled = false;
        }
      });
    }
  }

  return { fill, syncBotName, bindEvents };
}
