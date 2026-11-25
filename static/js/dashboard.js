/**
 * Điểm vào điều phối toàn bộ các tab tính năng, sidebar Oxfin và thanh tìm kiếm trên Web Dashboard.
 */

import { apiRequest, showToast } from "./core/api.js";
import { createAuditTabController } from "./features/audit/auditTab.js";
import { createAskConfigTabController } from "./features/config/askConfigTab.js";
import { createPromptConfigTabController } from "./features/config/promptConfigTab.js";
import { createModelsTabController } from "./features/models/modelsTab.js";
import { createStatusTabController } from "./features/monitoring/statusTab.js";
import { createPermissionsTabController } from "./features/permissions/permissionsTab.js";
import { createSessionsTabController } from "./features/sessions/sessionsTab.js";

function readBootstrapData() {
  const el = document.getElementById("dashboard-bootstrap");
  if (!el) {
    return { defaultSystemPrompt: "", defaultOcrInstruction: "", isAdmin: false };
  }
  return JSON.parse(el.textContent || "{}");
}

const bootstrap = readBootstrapData();

const statusTab = createStatusTabController();
const permissionsTab = createPermissionsTabController({ isAdmin: Boolean(bootstrap.isAdmin) });
const sessionsTab = createSessionsTabController();
const auditTab = createAuditTabController();

let askConfigTab;
let promptConfigTab;
let modelsTab;

async function loadConfig() {
  try {
    const data = await apiRequest("/api/config");
    const cfg = data.config;
    const updatedEl = document.getElementById("hdr-updated");
    if (updatedEl) {
      updatedEl.textContent = `Cập nhật: ${new Date(cfg.updated_at).toLocaleString("vi-VN")} · ${cfg.updated_by || "system"}`;
    }

    askConfigTab.fill(cfg);
    promptConfigTab.fill(cfg);
    modelsTab.fill(cfg, data.models_runtime);
  } catch (err) {
    showToast("Không tải được cấu hình: " + err.message, false);
  }
}

async function refreshAll() {
  await loadConfig();
  await statusTab.loadStatus(false);
}

askConfigTab = createAskConfigTabController({ onConfigSaved: loadConfig });
promptConfigTab = createPromptConfigTabController({
  defaultSystemPrompt: bootstrap.defaultSystemPrompt || "",
  defaultOcrInstruction: bootstrap.defaultOcrInstruction || "",
  onConfigSaved: loadConfig,
});
modelsTab = createModelsTabController({ onRefreshAll: refreshAll });

const VALID_TABS = new Set(["status", "ask", "prompt", "models", "perms", "sessions", "audit"]);
const SYSTEM_POLL_INTERVAL_MS = 1000;
const GEMINI_INITIAL_DELAY_MS = 200;
const GEMINI_POLL_INTERVAL_MS = 15000;
let activeTab = "status";
let systemPollTimer = null;
let geminiPollTimer = null;
let geminiInitialTimer = null;

// Kích hoạt tab theo mã tên, phát hiệu ứng chuyển trang mượt 200ms và đồng bộ URL hash
function activateTab(targetTab, { skipAutoLoad = false, updateHash = true } = {}) {
  const safeTab = VALID_TABS.has(targetTab) ? targetTab : "status";
  const isTabChanged = activeTab !== safeTab;
  activeTab = safeTab;

  const mainEl = document.querySelector("main");
  if (mainEl) {
    mainEl.dataset.activeTab = safeTab;
  }

  document.querySelectorAll(".tab-btn").forEach((item) => {
    item.dataset.active = String(item.dataset.tab === safeTab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    const isTarget = panel.id === `tab-${safeTab}`;
    panel.classList.toggle("hidden", !isTarget);
    if (isTarget) {
      // Khởi phát lại hiệu ứng mờ dần & trượt nhẹ lên mỗi khi chuyển tab (không chạy khi poll nền 1s)
      panel.classList.remove("ox-tab-enter");
      if (isTabChanged) {
        void panel.offsetWidth;
        panel.classList.add("ox-tab-enter");
      }
    }
  });
  document.body.classList.remove("sidebar-mobile-open");

  if (updateHash && window.location.hash !== `#${safeTab}`) {
    window.history.replaceState(null, "", `#${safeTab}`);
  }

  if (skipAutoLoad) return;
  if (safeTab === "perms") {
    permissionsTab.loadPermissions();
  } else if (safeTab === "sessions") {
    sessionsTab.searchSessions(0, { silent: false });
  } else if (safeTab === "audit") {
    auditTab.loadAuditLogs({ silent: false });
  }
}

// Nhịp 1000ms (1s): cập nhật nền im lặng RAM, CPU, Discord Ping, Uptime, bộ đếm & nhật ký đang mở
function runSystemPollTick() {
  if (document.hidden) return;
  statusTab.loadStatus(false, { silent: true });
  if (activeTab === "audit") {
    auditTab.loadAuditLogs({ silent: true });
  } else if (activeTab === "sessions") {
    sessionsTab.refreshCurrentSilently();
  }
}

// Nhịp 15000ms (15s): đo độ trễ Gemini API im lặng tránh chạm hạn mức 429 hoặc cooldown giả
function runGeminiLatencyTick() {
  if (document.hidden) return;
  statusTab.loadStatus(true, { silent: true });
}

function stopBackgroundPolling() {
  if (systemPollTimer !== null) {
    clearInterval(systemPollTimer);
    systemPollTimer = null;
  }
  if (geminiPollTimer !== null) {
    clearInterval(geminiPollTimer);
    geminiPollTimer = null;
  }
  if (geminiInitialTimer !== null) {
    clearTimeout(geminiInitialTimer);
    geminiInitialTimer = null;
  }
}

function startBackgroundPolling({ scheduleInitialGemini = false } = {}) {
  stopBackgroundPolling();
  const toggleEl = document.getElementById("auto-refresh-toggle");
  if (toggleEl && !toggleEl.checked) return;
  if (document.hidden) return;

  systemPollTimer = setInterval(runSystemPollTick, SYSTEM_POLL_INTERVAL_MS);
  geminiPollTimer = setInterval(runGeminiLatencyTick, GEMINI_POLL_INTERVAL_MS);

  if (scheduleInitialGemini) {
    geminiInitialTimer = setTimeout(() => {
      geminiInitialTimer = null;
      runGeminiLatencyTick();
    }, GEMINI_INITIAL_DELAY_MS);
  }
}

// Áp dụng hiệu ứng mờ dần & trượt nhẹ lên 1 lần duy nhất cho các thẻ khi tải trang đầu tiên
function applyInitialCardEntrance() {
  document.querySelectorAll("#tab-status .card, #tab-status .ox-kpi-card").forEach((el, idx) => {
    el.style.animationDelay = `${Math.min(idx * 35, 240)}ms`;
    el.classList.add("ox-enter");
  });
}

function bindShellControls() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.preventDefault();
      activateTab(btn.dataset.tab);
    });
  });

  window.addEventListener("hashchange", () => {
    const fromHash = window.location.hash.replace(/^#/, "");
    if (VALID_TABS.has(fromHash)) {
      activateTab(fromHash, { updateHash: false });
    }
  });

  // Bật / tắt chu kỳ tự làm mới (1s hệ thống + 15s Gemini) từ công tắc trên giao diện
  const autoToggle = document.getElementById("auto-refresh-toggle");
  if (autoToggle) {
    autoToggle.addEventListener("change", () => {
      if (autoToggle.checked) {
        startBackgroundPolling();
      } else {
        stopBackgroundPolling();
      }
    });
  }

  // Tự động tạm dừng cả 2 chu kỳ polling khi tab trình duyệt bị ẩn và tiếp tục ngay khi quay lại
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopBackgroundPolling();
    } else {
      const toggleEl = document.getElementById("auto-refresh-toggle");
      if (!toggleEl || toggleEl.checked) {
        runSystemPollTick();
        startBackgroundPolling();
      }
    }
  });

  // Nút thu gọn / mở rộng sidebar đáp ứng cả desktop và mobile
  const toggleBtn = document.getElementById("btn-toggle-sidebar");
  const backdrop = document.getElementById("sidebar-backdrop");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      if (window.innerWidth < 1024) {
        document.body.classList.toggle("sidebar-mobile-open");
      } else {
        document.body.classList.toggle("sidebar-collapsed");
      }
    });
  }
  if (backdrop) {
    backdrop.addEventListener("click", () => document.body.classList.remove("sidebar-mobile-open"));
  }

  // Nút làm mới và nút mở nhanh Nhật ký chỉnh sửa trên Topbar
  const topRefreshBtn = document.getElementById("btn-topbar-refresh");
  if (topRefreshBtn) {
    topRefreshBtn.addEventListener("click", () => refreshAll());
  }
  const topAuditBtn = document.getElementById("btn-topbar-audit");
  if (topAuditBtn) {
    topAuditBtn.addEventListener("click", () => activateTab("audit"));
  }

  // Ô tìm kiếm nhanh trên Topbar: nhấn Enter để nhảy tab hoặc tra cứu trực tiếp phiên chat
  const globalSearch = document.getElementById("global-search");
  if (globalSearch) {
    globalSearch.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const raw = globalSearch.value.trim();
      const q = raw.toLowerCase();
      if (!q) return;
      if (q.includes("gemini") || q.includes("model") || q.includes("flash") || q.includes("pro")) {
        activateTab("models");
        const modSearch = document.getElementById("mod-search");
        if (modSearch) {
          modSearch.value = raw;
          modSearch.dispatchEvent(new Event("input"));
        }
      } else if (q.includes("prompt") || q.includes("ocr") || q.includes("system")) {
        activateTab("prompt");
      } else if (q.includes("ask") || q.includes("cooldown")) {
        activateTab("ask");
      } else if (q.includes("perm") || q.includes("admin") || q.includes("user") || q.includes("quyền")) {
        activateTab("perms");
      } else if (q.includes("audit") || q.includes("log") || q.includes("nhật ký")) {
        activateTab("audit");
      } else {
        // Mặc định tìm kiếm trực tiếp trong nội dung phiên chat trên MongoDB Atlas
        activateTab("sessions", { skipAutoLoad: true });
        sessionsTab.setQueryAndSearch(raw);
      }
    });
  }
}

// Khởi chạy toàn bộ trình lắng nghe sự kiện, hiệu ứng lần đầu và vòng lặp nền 2000ms
bindShellControls();
statusTab.bindEvents();
askConfigTab.bindEvents();
promptConfigTab.bindEvents();
modelsTab.bindEvents();
permissionsTab.bindEvents();
sessionsTab.bindEvents();
auditTab.bindEvents();

applyInitialCardEntrance();

// Khôi phục tab đang mở từ URL hash (tránh bị quay về trang chủ khi tải lại)
const initialTab = window.location.hash.replace(/^#/, "");
if (VALID_TABS.has(initialTab) && initialTab !== "status") {
  activateTab(initialTab, { updateHash: false });
}

statusTab.loadStatus(false, { silent: false });
loadConfig();
startBackgroundPolling({ scheduleInitialGemini: true });
