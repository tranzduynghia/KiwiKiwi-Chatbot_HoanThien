/**
 * Bộ điều khiển thẻ "Bot Presence & Status":
 * - Chọn trạng thái trực tuyến (online / idle / dnd / invisible) kèm chấm màu
 * - Tùy chỉnh Custom Status (bong bóng chữ) và Hoạt động chính (None / Playing / Listening to / Watching / Competing in)
 * - Chế độ Smart Merge (mặc định khi điền cả 2): gộp thành 1 bong bóng chữ `Custom · Prefix Activity` để Discord không ẩn
 * - Tùy chọn "Luân phiên tự động (mỗi 20s)": bot đổi qua lại giữa bong bóng chữ và hoạt động chính mỗi 20 giây
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

const PREVIEW_ROTATE_MS = 3500;

export function createPresenceCardController({ onConfigSaved }) {
  let selectedStatus = "online";
  let previewRotateStep = 0;
  let previewRotateTimer = null;

  function stopPreviewRotation() {
    if (previewRotateTimer) {
      clearInterval(previewRotateTimer);
      previewRotateTimer = null;
    }
  }

  function ensurePreviewRotation(shouldRotate) {
    if (!shouldRotate) {
      stopPreviewRotation();
      previewRotateStep = 0;
      return;
    }
    if (!previewRotateTimer) {
      previewRotateTimer = setInterval(() => {
        if (document.hidden) return;
        previewRotateStep = (previewRotateStep + 1) % 2;
        renderPreview();
      }, PREVIEW_ROTATE_MS);
    }
  }

  function updateStatusButtons() {
    document.querySelectorAll(".presence-status-btn").forEach((btn) => {
      btn.dataset.selected = String(btn.dataset.presenceStatus === selectedStatus);
    });
  }

  function setCustomBubble(customWrapEl, customTextEl, text) {
    if (!customWrapEl || !customTextEl) return;
    if (text) {
      customWrapEl.classList.remove("hidden");
      customTextEl.textContent = text;
    } else {
      customWrapEl.classList.add("hidden");
      customTextEl.textContent = "";
    }
  }

  function renderPreview() {
    const customEl = document.getElementById("presence-custom-status");
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    const rotateEl = document.getElementById("presence-auto-rotate");
    const modeHintEl = document.getElementById("presence-mode-hint");
    const modeBadgeEl = document.getElementById("presence-preview-mode-badge");
    const dotEl = document.getElementById("presence-preview-dot");
    const customWrapEl = document.getElementById("presence-preview-custom-wrap");
    const customTextEl = document.getElementById("presence-preview-custom-text");
    const activityEl = document.getElementById("presence-preview-activity");
    const statusLabelEl = document.getElementById("presence-preview-status-label");
    if (!typeEl || !textEl || !dotEl || !activityEl || !statusLabelEl) return;

    const customVal = customEl ? customEl.value.trim() : "";
    const activityType = typeEl.value || "none";
    const textVal = textEl.value.trim();
    const autoRotate = Boolean(rotateEl && rotateEl.checked);
    textEl.disabled = activityType === "none";

    const meta = STATUS_META[selectedStatus] || STATUS_META.online;
    dotEl.className = `w-3.5 h-3.5 rounded-full ${meta.dotClass} border-2 border-[#0a0d12] absolute -bottom-0.5 -right-0.5 transition-colors duration-200`;
    statusLabelEl.textContent = meta.label;

    const hasCustom = Boolean(customVal);
    const hasPrimary = activityType !== "none" && Boolean(textVal);
    const prefix = ACTIVITY_PREFIX[activityType] || "Listening to";

    if (modeHintEl) {
      modeHintEl.textContent = autoRotate
        ? "Đang bật: Bot sẽ tự đổi qua lại giữa Bong bóng Custom Status và Hoạt động chính mỗi 20 giây."
        : "Tắt (mặc định): Gộp thông minh thành 1 bong bóng chữ (Custom Status · Hoạt động) để Discord không ẩn hoạt động chính.";
    }

    // Trường hợp điền CẢ HAI: áp dụng Smart Merge hoặc Auto-Rotate 20s
    if (hasCustom && hasPrimary) {
      if (autoRotate) {
        ensurePreviewRotation(true);
        if (previewRotateStep === 0) {
          setCustomBubble(customWrapEl, customTextEl, customVal);
          activityEl.classList.add("hidden");
          activityEl.innerHTML = "";
          if (modeBadgeEl) modeBadgeEl.textContent = "🔄 Luân phiên 20s · 1/2 Bong bóng";
        } else {
          setCustomBubble(customWrapEl, customTextEl, "");
          activityEl.classList.remove("hidden");
          activityEl.innerHTML = `${escapeHtml(prefix)} <strong class="text-slate-200 font-medium">${escapeHtml(textVal)}</strong>`;
          if (modeBadgeEl) modeBadgeEl.textContent = "🔄 Luân phiên 20s · 2/2 Hoạt động";
        }
        return;
      }

      // Smart Merge mặc định: gộp thành 1 bong bóng chữ phân tách bằng " · "
      ensurePreviewRotation(false);
      const mergedText = `${customVal} · ${prefix} ${textVal}`.slice(0, 128);
      setCustomBubble(customWrapEl, customTextEl, mergedText);
      activityEl.classList.add("hidden");
      activityEl.innerHTML = "";
      if (modeBadgeEl) modeBadgeEl.textContent = "✨ Smart Merge (1 bong bóng)";
      return;
    }

    ensurePreviewRotation(false);

    if (hasCustom) {
      setCustomBubble(customWrapEl, customTextEl, customVal);
      activityEl.classList.add("hidden");
      activityEl.innerHTML = "";
      if (modeBadgeEl) modeBadgeEl.textContent = "💬 Chỉ Bong bóng chữ";
      return;
    }

    if (hasPrimary) {
      setCustomBubble(customWrapEl, customTextEl, "");
      activityEl.classList.remove("hidden");
      activityEl.innerHTML = `${escapeHtml(prefix)} <strong class="text-slate-200 font-medium">${escapeHtml(textVal)}</strong>`;
      if (modeBadgeEl) modeBadgeEl.textContent = "🎮 Hoạt động chính";
      return;
    }

    setCustomBubble(customWrapEl, customTextEl, "");
    activityEl.classList.remove("hidden");
    activityEl.innerHTML = '<span class="text-[#5e6778] italic">Không hiển thị hoạt động</span>';
    if (modeBadgeEl) modeBadgeEl.textContent = "Không hoạt động";
  }

  function fill(config) {
    const presence = config?.presence || {};
    selectedStatus = STATUS_META[presence.status] ? presence.status : "online";

    const customEl = document.getElementById("presence-custom-status");
    const typeEl = document.getElementById("presence-activity-type");
    const textEl = document.getElementById("presence-activity-text");
    const rotateEl = document.getElementById("presence-auto-rotate");

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
    if (rotateEl) rotateEl.checked = Boolean(presence.auto_rotate);

    previewRotateStep = 0;
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
    const rotateEl = document.getElementById("presence-auto-rotate");
    if (customEl) customEl.addEventListener("input", renderPreview);
    if (typeEl) typeEl.addEventListener("change", renderPreview);
    if (textEl) textEl.addEventListener("input", renderPreview);
    if (rotateEl) {
      rotateEl.addEventListener("change", () => {
        previewRotateStep = 0;
        renderPreview();
      });
    }

    const saveBtn = document.getElementById("btn-save-presence");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const customStatus = customEl ? customEl.value.trim() : "";
        const activityType = typeEl ? typeEl.value : "none";
        const activityText = textEl ? textEl.value.trim() : "";
        const autoRotate = Boolean(rotateEl && rotateEl.checked);
        saveBtn.disabled = true;
        try {
          const res = await apiRequest("/api/config/presence", {
            method: "PUT",
            body: JSON.stringify({
              status: selectedStatus,
              custom_status: customStatus,
              activity_type: activityType,
              activity_text: activityText,
              auto_rotate: autoRotate,
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
