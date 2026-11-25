/**
 * Bộ điều khiển thẻ "Bot Presence & Status":
 * - Chọn trạng thái trực tuyến (online / idle / dnd / invisible) kèm chấm màu
 * - Chọn loại hoạt động (Playing / Listening to / Watching / Competing in / Custom Status / None)
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
  custom: "",
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
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    const dotEl = document.getElementById("presence-preview-dot");
    const activityEl = document.getElementById("presence-preview-activity");
    const statusLabelEl = document.getElementById("presence-preview-status-label");
    if (!typeEl || !textEl || !dotEl || !activityEl || !statusLabelEl) return;

    const activityType = typeEl.value || "listening";
    const textVal = textEl.value.trim();
    textEl.disabled = activityType === "none";

    const meta = STATUS_META[selectedStatus] || STATUS_META.online;
    dotEl.className = `w-3.5 h-3.5 rounded-full ${meta.dotClass} border-2 border-[#0a0d12] absolute -bottom-0.5 -right-0.5 transition-colors duration-200`;
    statusLabelEl.textContent = meta.label;

    if (activityType === "none" || !textVal) {
      activityEl.innerHTML = '<span class="text-[#5e6778] italic">Không hiển thị hoạt động</span>';
    } else if (activityType === "custom") {
      activityEl.innerHTML = `<span class="text-slate-200">${escapeHtml(textVal)}</span>`;
    } else {
      const prefix = ACTIVITY_PREFIX[activityType] || "Listening to";
      activityEl.innerHTML = `${escapeHtml(prefix)} <strong class="text-slate-200 font-medium">${escapeHtml(textVal)}</strong>`;
    }
  }

  function fill(config) {
    const presence = config?.presence || {};
    selectedStatus = STATUS_META[presence.status] ? presence.status : "online";
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    if (typeEl) typeEl.value = presence.activity_type || "listening";
    if (textEl) textEl.value = presence.activity_text ?? "m!help";

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

    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    if (typeEl) typeEl.addEventListener("change", renderPreview);
    if (textEl) textEl.addEventListener("input", renderPreview);

    const saveBtn = document.getElementById("btn-save-presence");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const activityType = typeEl ? typeEl.value : "listening";
        const activityText = textEl ? textEl.value.trim() : "";
        saveBtn.disabled = true;
        try {
          const res = await apiRequest("/api/config/presence", {
            method: "PUT",
            body: JSON.stringify({
              status: selectedStatus,
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
