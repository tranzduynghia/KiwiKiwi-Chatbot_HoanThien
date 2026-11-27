/**
 * Bộ điều khiển Step-up Authentication phía giao diện:
 * - Theo dõi tương tác người dùng thực (pointerdown, throttled mousemove, keydown, scroll, touchstart) bằng delta Date.now()
 * - Tự động khóa sau 60 giây nhàn rỗi hoặc khi bấm "Khóa ngay"
 * - Gọi POST /api/auth/step-up/lock, xóa sạch state JS & hủy DOM qua replaceChildren()
 * - Hiển thị bộ đếm ngược thời gian thực ("Tự khóa sau 58s") + nút "Khóa ngay" khi đã mở khóa
 */

import { apiRequest, showToast } from "./api.js";
import { closeModalAnimated, openModalAnimated } from "./modalTransition.js";

const TAB_LABELS = {
  perms: "Phân quyền & Admin",
  sessions: "Tra cứu phiên chat",
  temp: "Trang nháp (/temp)",
  "temp-page": "Trang nháp (/temp)",
  "dashboard-settings": "Cấu hình Dashboard",
  legal: "Điều khoản & Pháp lý",
  audit: "Nhật ký chỉnh sửa",
  ask: "Cấu hình /ask",
  prompt: "System Instruction",
  models: "Model Gemini",
  status: "Trạng thái Bot",
};

const ERROR_MESSAGES = {
  cancelled: "Bạn đã hủy xác thực lại bằng Discord.",
  missing_params: "Thiếu tham số xác thực từ Discord OAuth2.",
  invalid_state: "Trạng thái OAuth2 không hợp lệ hoặc đã hết hạn. Vui lòng thử lại.",
  session_mismatch: "Phiên đăng nhập không khớp với yêu cầu xác thực.",
  oauth_failed: "Không thể xác thực với Discord OAuth2. Vui lòng thử lại.",
  discord_mismatch: "Tài khoản Discord vừa đăng nhập KHÔNG trùng khớp với tài khoản đang đăng nhập Dashboard.",
  not_discord: "Phiên hiện tại không sử dụng đăng nhập Discord.",
  oauth_not_configured: "Discord OAuth2 chưa được cấu hình trên máy chủ.",
};

export function createStepUpController({
  authType = "password",
  timeoutSeconds = 60,
  lockedSections = ["perms", "sessions", "temp"],
  getActiveTab,
  onLockAll,
  onUnlockedTab,
}) {
  let currentTimeoutSec = Math.max(15, Math.min(300, Number(timeoutSeconds) || 60));
  let idleTimeoutMs = currentTimeoutSec * 1000;
  let lockedSet = new Set(Array.isArray(lockedSections) ? lockedSections.map((s) => String(s).toLowerCase()) : ["perms", "sessions", "temp"]);
  let unlocked = false;
  let lastActivityAt = 0;
  let countdownInterval = null;
  let lastMouseMoveAt = 0;
  let activityListenersAttached = false;
  let pendingTargetTab = "perms";
  let pendingAfterUnlock = null;
  let modalTransitionToken = 0;
  let modalCloseTimer = null;
  let modalCloseCleanup = null;

  function isSectionLocked(tab) {
    if (tab === "dashboard-settings" || tab === "temp-page") return true;
    return lockedSet.has(String(tab || "").toLowerCase());
  }

  function isSensitiveTab(tab) {
    return isSectionLocked(tab);
  }

  function setTimeoutSeconds(sec) {
    currentTimeoutSec = Math.max(15, Math.min(300, Number(sec) || 60));
    idleTimeoutMs = currentTimeoutSec * 1000;
    if (unlocked) {
      lastActivityAt = Date.now();
      updateCountdownBadges();
    }
  }

  function setLockedSections(list) {
    lockedSet = new Set(Array.isArray(list) ? list.map((s) => String(s).toLowerCase()) : ["perms", "sessions", "temp"]);
  }

  function getRemainingSeconds() {
    if (!unlocked || lastActivityAt <= 0) return 0;
    const elapsedMs = Math.max(0, Date.now() - lastActivityAt);
    return Math.max(0, Math.ceil((idleTimeoutMs - elapsedMs) / 1000));
  }

  function updateCountdownBadges() {
    const remaining = getRemainingSeconds();
    document.querySelectorAll("[data-stepup-countdown]").forEach((el) => {
      el.textContent = `Tự khóa sau ${remaining}s`;
    });
  }

  function renderStepUpBars() {
    const barIds = ["perms-stepup-bar", "sessions-stepup-bar", "temp-stepup-bar", "dashboard-settings-stepup-bar"];
    const remaining = getRemainingSeconds();
    barIds.forEach((id) => {
      const bar = document.getElementById(id);
      if (!bar) return;
      bar.replaceChildren();
      if (!unlocked) return;

      const badge = document.createElement("span");
      badge.className = "stepup-countdown-badge font-mono";
      badge.setAttribute("aria-live", "polite");

      const dot = document.createElement("span");
      dot.className = "inline-block w-2 h-2 rounded-full bg-amber-400";
      dot.setAttribute("aria-hidden", "true");

      const text = document.createElement("span");
      text.dataset.stepupCountdown = "true";
      text.textContent = `Tự khóa sau ${remaining}s`;

      badge.appendChild(dot);
      badge.appendChild(text);

      const lockBtn = document.createElement("button");
      lockBtn.type = "button";
      lockBtn.className = "stepup-lock-btn";
      lockBtn.textContent = "Khóa ngay";
      lockBtn.addEventListener("click", () => {
        lock({ notifyServer: true, openModalIfSensitive: true, reason: "manual" });
      });

      bar.appendChild(badge);
      bar.appendChild(lockBtn);
    });
  }

  function onRealActivity(event) {
    if (!unlocked || lastActivityAt <= 0) return;
    const now = Date.now();
    if (event && event.type === "mousemove") {
      if (now - lastMouseMoveAt < 500) return;
      lastMouseMoveAt = now;
    }
    if (now - lastActivityAt >= idleTimeoutMs) {
      lock({ notifyServer: true, openModalIfSensitive: true, reason: "timeout" });
      return;
    }
    lastActivityAt = now;
    updateCountdownBadges();
  }

  function attachActivityListeners() {
    if (activityListenersAttached) return;
    activityListenersAttached = true;
    window.addEventListener("pointerdown", onRealActivity, { passive: true });
    window.addEventListener("mousemove", onRealActivity, { passive: true });
    window.addEventListener("keydown", onRealActivity, { passive: true });
    window.addEventListener("scroll", onRealActivity, { passive: true, capture: true });
    window.addEventListener("touchstart", onRealActivity, { passive: true });
  }

  function detachActivityListeners() {
    if (!activityListenersAttached) return;
    activityListenersAttached = false;
    window.removeEventListener("pointerdown", onRealActivity);
    window.removeEventListener("mousemove", onRealActivity);
    window.removeEventListener("keydown", onRealActivity);
    window.removeEventListener("scroll", onRealActivity, true);
    window.removeEventListener("touchstart", onRealActivity);
  }

  function stopCountdownTimer() {
    if (countdownInterval !== null) {
      clearInterval(countdownInterval);
      countdownInterval = null;
    }
  }

  function startCountdownTimer() {
    stopCountdownTimer();
    countdownInterval = setInterval(() => {
      if (!unlocked || lastActivityAt <= 0) {
        stopCountdownTimer();
        return;
      }
      const elapsedMs = Date.now() - lastActivityAt;
      if (elapsedMs >= idleTimeoutMs) {
        lock({ notifyServer: true, openModalIfSensitive: true, reason: "timeout" });
        return;
      }
      updateCountdownBadges();
    }, 500);
  }

  function showModalError(message) {
    const errEl = document.getElementById("stepup-error");
    if (!errEl) return;
    if (!message) {
      errEl.textContent = "";
      errEl.classList.add("hidden");
      return;
    }
    errEl.textContent = message;
    errEl.classList.remove("hidden");
  }

  function clearPendingModalClose() {
    if (modalCloseTimer !== null) {
      clearTimeout(modalCloseTimer);
      modalCloseTimer = null;
    }
    if (modalCloseCleanup) {
      modalCloseCleanup();
      modalCloseCleanup = null;
    }
  }

  function finalizeCloseModal(modal) {
    clearPendingModalClose();
    modal.classList.add("hidden");
    modal.classList.remove("flex", "is-open", "is-closing");
    const pwdInput = document.getElementById("stepup-password-input");
    if (pwdInput) pwdInput.value = "";
    showModalError("");
  }

  function closeModal() {
    const modal = document.getElementById("stepup-modal");
    if (!modal || modal.classList.contains("hidden") || modal.classList.contains("is-closing")) return;

    const prefersReducedMotion =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (prefersReducedMotion) {
      modalTransitionToken += 1;
      finalizeCloseModal(modal);
      return;
    }

    clearPendingModalClose();
    const myToken = ++modalTransitionToken;
    const panel = document.getElementById("stepup-modal-panel");
    const target = panel || modal;

    modal.classList.remove("is-open");
    modal.classList.add("is-closing");

    let finished = false;
    const onDone = () => {
      if (finished) return;
      finished = true;
      if (modalTransitionToken !== myToken) return;
      finalizeCloseModal(modal);
    };

    const onTransitionEnd = (event) => {
      if (event.target !== target) return;
      onDone();
    };

    target.addEventListener("transitionend", onTransitionEnd);
    modalCloseCleanup = () => target.removeEventListener("transitionend", onTransitionEnd);
    modalCloseTimer = setTimeout(onDone, 180);
  }

  function openModal(targetTab, { errorMessage = "", afterUnlock = null } = {}) {
    const safeTab = TAB_LABELS[targetTab] ? targetTab : "perms";
    pendingTargetTab = safeTab;
    pendingAfterUnlock = typeof afterUnlock === "function" ? afterUnlock : null;

    // Đảm bảo bộ đếm 60s KHÔNG chạy hoặc tiến triển khi đang mở modal / chưa xác thực
    if (!unlocked) {
      lastActivityAt = 0;
      stopCountdownTimer();
      detachActivityListeners();
    }

    const modal = document.getElementById("stepup-modal");
    const targetLabel = document.getElementById("stepup-target-label");
    const discordBtn = document.getElementById("btn-stepup-discord");
    const pwdForm = document.getElementById("stepup-password-form");
    const discordWrap = document.getElementById("stepup-discord-wrap");
    const pwdInput = document.getElementById("stepup-password-input");

    if (targetLabel) {
      targetLabel.textContent = TAB_LABELS[safeTab] || TAB_LABELS.perms;
    }
    if (discordBtn) {
      discordBtn.setAttribute("href", `/api/auth/step-up/discord?tab=${encodeURIComponent(safeTab)}`);
    }
    if (pwdForm && discordWrap) {
      const isDiscord = authType === "discord";
      pwdForm.classList.toggle("hidden", isDiscord);
      discordWrap.classList.toggle("hidden", !isDiscord);
    }

    showModalError(errorMessage);

    if (modal) {
      clearPendingModalClose();
      modalTransitionToken += 1;
      const myToken = modalTransitionToken;
      const panel = document.getElementById("stepup-modal-panel");
      modal.classList.remove("hidden", "is-closing", "is-open");
      modal.classList.add("flex");
      if (panel) {
        panel.classList.remove("ox-modal-enter");
        void panel.offsetWidth;
        panel.classList.add("ox-modal-enter");
      }
      void modal.offsetWidth;
      requestAnimationFrame(() => {
        if (modalTransitionToken !== myToken) return;
        modal.classList.add("is-open");
      });
    }

    if (authType !== "discord" && pwdInput) {
      pwdInput.value = "";
      requestAnimationFrame(() => pwdInput.focus());
    }
  }

  function lock({ notifyServer = true, openModalIfSensitive = false, reason = "manual" } = {}) {
    const wasUnlocked = unlocked;
    unlocked = false;
    lastActivityAt = 0;
    stopCountdownTimer();
    detachActivityListeners();

    // Xóa sạch thanh đếm ngược trên tất cả các tab nhạy cảm
    ["perms-stepup-bar", "sessions-stepup-bar", "temp-stepup-bar", "dashboard-settings-stepup-bar"].forEach((id) => {
      const bar = document.getElementById(id);
      if (bar) bar.replaceChildren();
    });

    // Đóng modal xác nhận lưu nếu đang mở
    const confirmModal = document.getElementById("confirm-save-modal");
    if (confirmModal && !confirmModal.classList.contains("hidden")) {
      confirmModal.classList.add("hidden");
      confirmModal.classList.remove("flex");
    }

    // Xóa sạch toàn bộ state JS và hủy DOM của cả 2 tab nhạy cảm qua replaceChildren()
    if (typeof onLockAll === "function") {
      onLockAll();
    }

    if (notifyServer) {
      fetch("/api/auth/step-up/lock", {
        method: "POST",
        credentials: "same-origin",
        redirect: "manual",
        headers: { "Content-Type": "application/json" },
      }).catch(() => {});
    }

    if (wasUnlocked && reason === "timeout") {
      showToast("Đã tự động khóa các mục nhạy cảm sau 60 giây không hoạt động.", false);
    } else if (wasUnlocked && reason === "manual") {
      showToast("Đã khóa ngay các mục nhạy cảm.", true);
    }

    const currentTab = typeof getActiveTab === "function" ? getActiveTab() : "";
    if (openModalIfSensitive && isSensitiveTab(currentTab)) {
      const msg =
        reason === "timeout"
          ? "Phiên mở khóa đã hết hạn sau 60 giây nhàn rỗi. Vui lòng xác thực lại."
          : reason === "server_403"
            ? "Phiên xác thực đã hết hạn trên máy chủ. Vui lòng xác thực lại."
            : "";
      openModal(currentTab, { errorMessage: msg });
    }
  }

  function unlock({ remainingSeconds = currentTimeoutSec, targetTab = null, freshAuth = false } = {}) {
    const now = Date.now();
    const clampedRemaining = Math.max(1, Math.min(currentTimeoutSec, Number(remainingSeconds) || currentTimeoutSec));
    unlocked = true;
    lastActivityAt = freshAuth
      ? now
      : now - Math.max(0, (currentTimeoutSec - clampedRemaining) * 1000);
    lastMouseMoveAt = 0;
    closeModal();
    attachActivityListeners();
    startCountdownTimer();
    renderStepUpBars();

    const resolvedTab = targetTab || pendingTargetTab || (typeof getActiveTab === "function" ? getActiveTab() : "perms");
    const callback = pendingAfterUnlock;
    pendingAfterUnlock = null;

    if (typeof onUnlockedTab === "function" && isSensitiveTab(resolvedTab)) {
      onUnlockedTab(resolvedTab, callback);
    } else if (typeof callback === "function") {
      callback();
    }
  }

  async function syncStatusFromServer(targetTab) {
    try {
      const data = await apiRequest("/api/auth/step-up/status");
      if (data && data.unlocked && Number(data.remaining_seconds) > 0) {
        unlock({ remainingSeconds: Number(data.remaining_seconds), targetTab });
        return true;
      }
    } catch {
      // Bỏ qua lỗi mạng khi kiểm tra trạng thái; giữ nguyên trạng thái khóa
    }
    return false;
  }

  function handleCallbackQueryParams() {
    const url = new URL(window.location.href);
    const stepUpStatus = url.searchParams.get("step_up");
    const stepUpError = url.searchParams.get("step_up_error");
    if (!stepUpStatus && !stepUpError) {
      return { status: null, errorMessage: "" };
    }
    url.searchParams.delete("step_up");
    url.searchParams.delete("step_up_error");
    const cleanUrl = `${url.pathname}${url.search}${url.hash}`;
    window.history.replaceState(null, "", cleanUrl);

    if (stepUpError) {
      const msg = ERROR_MESSAGES[stepUpError] || "Xác thực lại qua Discord thất bại. Vui lòng thử lại.";
      return { status: "error", errorMessage: msg };
    }
    return { status: stepUpStatus, errorMessage: "" };
  }

  function bindEvents() {
    const form = document.getElementById("stepup-password-form");
    const submitBtn = document.getElementById("btn-submit-stepup-password");
    const submitText = document.getElementById("stepup-password-btn-text");
    const pwdInput = document.getElementById("stepup-password-input");

    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const password = pwdInput ? pwdInput.value : "";
        if (!password) {
          showModalError("Vui lòng nhập mật khẩu để tiếp tục.");
          return;
        }
        showModalError("");
        if (submitBtn) submitBtn.disabled = true;
        if (submitText) submitText.textContent = "Đang xác thực...";

        try {
          const data = await apiRequest("/api/auth/step-up/password", {
            method: "POST",
            body: JSON.stringify({ password }),
            redirectOn401: false,
          });
          showToast("Đã xác thực thành công. Tự động khóa sau 60 giây nhàn rỗi.", true);
          unlock({
            remainingSeconds: Number(data.expires_in) || timeoutSeconds,
            targetTab: pendingTargetTab,
            freshAuth: true,
          });
        } catch (err) {
          showModalError(err.message || "Mật khẩu xác thực không chính xác.");
          if (pwdInput) {
            pwdInput.value = "";
            pwdInput.focus();
          }
        } finally {
          if (submitBtn) submitBtn.disabled = false;
          if (submitText) submitText.textContent = "Xác nhận mở khóa";
        }
      });
    }

    const closeBtn = document.getElementById("btn-close-stepup-modal");
    const cancelBtn = document.getElementById("btn-cancel-stepup");
    const cancelDiscordBtn = document.getElementById("btn-cancel-stepup-discord");
    const modal = document.getElementById("stepup-modal");

    if (closeBtn) closeBtn.addEventListener("click", closeModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
    if (cancelDiscordBtn) cancelDiscordBtn.addEventListener("click", closeModal);
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeModal();
      });
    }
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.classList.contains("hidden") && !modal.classList.contains("is-closing")) {
        closeModal();
      }
    });

    // Kiểm tra lại mốc thời gian delta Date.now() ngay khi quay lại tab trình duyệt
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && unlocked && lastActivityAt > 0) {
        if (Date.now() - lastActivityAt >= idleTimeoutMs) {
          lock({ notifyServer: true, openModalIfSensitive: true, reason: "timeout" });
        } else {
          updateCountdownBadges();
        }
      }
    });
  }

  return {
    isUnlocked: () => unlocked,
    isSectionLocked,
    setTimeoutSeconds,
    setLockedSections,
    openModal,
    closeModal,
    lock,
    unlock,
    syncStatusFromServer,
    handleCallbackQueryParams,
    bindEvents,
  };
}
