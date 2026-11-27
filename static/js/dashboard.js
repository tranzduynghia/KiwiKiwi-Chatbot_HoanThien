/**
 * Điểm vào điều phối toàn bộ các tab tính năng, sidebar Oxfin, thanh tìm kiếm và Step-up Auth trên Web Dashboard.
 * - Zero Prefetch: KHÔNG bao giờ gọi API cho tab 'Phân quyền & Admin' hoặc 'Tra cứu phiên chat' khi chưa xác thực Step-up.
 * - Background polling 1s chỉ cập nhật RAM/CPU/Ping/Audit (KHÔNG bao giờ chạm endpoint nhạy cảm).
 */

import { apiRequest, setStepUpRequiredHandler, showToast } from "./core/api.js";
import { createStepUpController } from "./core/stepUp.js";
import { initThemeSwitcher } from "./core/themeSwitcher.js";
import { createAuditTabController } from "./features/audit/auditTab.js";
import { createAskConfigTabController } from "./features/config/askConfigTab.js";
import { createPresenceCardController } from "./features/config/presenceCard.js";
import { createPromptConfigTabController } from "./features/config/promptConfigTab.js";
import { createTunnelCardController } from "./features/config/tunnelCard.js";
import { createLegalCmsTabController, initWebTosConsentGate } from "./features/legal/legalCmsTab.js";
import { createModelsTabController } from "./features/models/modelsTab.js";
import { createStatusTabController } from "./features/monitoring/statusTab.js";
import { createPermissionsTabController } from "./features/permissions/permissionsTab.js";
import { createSessionsTabController } from "./features/sessions/sessionsTab.js";

function readBootstrapData() {
  const el = document.getElementById("dashboard-bootstrap");
  if (!el) {
    return {
      defaultSystemPrompt: "",
      defaultOcrInstruction: "",
      isAdmin: false,
      authType: "password",
      discordOauthEnabled: false,
      stepUpTimeoutSeconds: 60,
    };
  }
  return JSON.parse(el.textContent || "{}");
}

const bootstrap = readBootstrapData();

const VALID_TABS = new Set(["status", "ask", "prompt", "models", "perms", "sessions", "legal", "audit"]);
const TAB_ALIASES = {
  admin: "perms",
  permissions: "perms",
  session: "sessions",
};
const SENSITIVE_TABS = new Set(["perms", "sessions"]);

function normalizeTabName(rawTab) {
  const cleaned = String(rawTab || "").trim().toLowerCase().replace(/^#/, "");
  const mapped = TAB_ALIASES[cleaned] || cleaned;
  return VALID_TABS.has(mapped) ? mapped : "status";
}

const SYSTEM_POLL_INTERVAL_MS = 1000;
const GEMINI_INITIAL_DELAY_MS = 200;
const GEMINI_POLL_INTERVAL_MS = 15000;
let activeTab = "status";
let systemPollTimer = null;
let geminiPollTimer = null;
let geminiInitialTimer = null;

let tunnelCard;
let permissionsTab;
let sessionsTab;

const statusTab = createStatusTabController({
  onTunnelTelemetry: (telemetry) => {
    if (tunnelCard) tunnelCard.renderTelemetry(telemetry);
  },
});
const legalCmsTab = createLegalCmsTabController({ isAdmin: Boolean(bootstrap.isAdmin) });
const auditTab = createAuditTabController();

const stepUp = createStepUpController({
  authType: bootstrap.authType || "password",
  timeoutSeconds: Number(bootstrap.stepUpTimeoutSeconds) || 60,
  getActiveTab: () => activeTab,
  onLockAll: () => {
    if (permissionsTab) permissionsTab.destroyAndLock();
    if (sessionsTab) sessionsTab.destroyAndLock();
  },
  onUnlockedTab: (tab, afterUnlock) => {
    if (tab === "perms") {
      permissionsTab.loadPermissions();
    } else if (tab === "sessions") {
      if (typeof afterUnlock === "function") {
        afterUnlock();
      } else {
        sessionsTab.searchSessions(0);
      }
    }
  },
});

setStepUpRequiredHandler(() => {
  stepUp.lock({ notifyServer: false, openModalIfSensitive: true, reason: "server_403" });
});

permissionsTab = createPermissionsTabController({
  isAdmin: Boolean(bootstrap.isAdmin),
  isStepUpUnlocked: () => stepUp.isUnlocked(),
  onRequestUnlock: (tab) => stepUp.openModal(tab),
});

sessionsTab = createSessionsTabController({
  isStepUpUnlocked: () => stepUp.isUnlocked(),
  onRequestUnlock: (tab) => stepUp.openModal(tab),
});

let askConfigTab;
let presenceCard;
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
    presenceCard.fill(cfg);
    tunnelCard.fill(cfg);
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
presenceCard = createPresenceCardController({ onConfigSaved: loadConfig });
tunnelCard = createTunnelCardController({
  isAdmin: Boolean(bootstrap.isAdmin),
  onConfigSaved: loadConfig,
});
promptConfigTab = createPromptConfigTabController({
  defaultSystemPrompt: bootstrap.defaultSystemPrompt || "",
  defaultOcrInstruction: bootstrap.defaultOcrInstruction || "",
  onConfigSaved: loadConfig,
});
modelsTab = createModelsTabController({ onRefreshAll: refreshAll });

function activateTab(
  targetTab,
  { skipAutoLoad = false, updateHash = true, afterUnlock = null, stepUpError = "", suppressModal = false } = {}
) {
  const safeTab = normalizeTabName(targetTab);
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

  if (SENSITIVE_TABS.has(safeTab)) {
    if (!stepUp.isUnlocked()) {
      if (!suppressModal) {
        stepUp.openModal(safeTab, { errorMessage: stepUpError, afterUnlock });
      }
      return;
    }
    if (skipAutoLoad) return;
    if (safeTab === "perms") {
      permissionsTab.loadPermissions();
    } else if (safeTab === "sessions") {
      if (typeof afterUnlock === "function") {
        afterUnlock();
      } else {
        sessionsTab.searchSessions(0);
      }
    }
    return;
  }

  if (skipAutoLoad) return;
  if (safeTab === "legal") {
    legalCmsTab.onTabActivated();
  } else if (safeTab === "audit") {
    auditTab.loadAuditLogs({ silent: false });
  }
}

// Nhịp 1000ms (1s): CHỈ cập nhật RAM, CPU, Discord Ping, Uptime & Audit (TUYỆT ĐỐI không poll mục nhạy cảm)
function runSystemPollTick() {
  if (document.hidden) return;
  statusTab.loadStatus(false, { silent: true });
  if (activeTab === "audit") {
    auditTab.loadAuditLogs({ silent: true });
  }
}

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
    const rawHash = window.location.hash.replace(/^#/, "");
    if (!rawHash) return;
    const normalized = normalizeTabName(rawHash);
    if (VALID_TABS.has(normalized)) {
      activateTab(normalized, { updateHash: normalized !== rawHash });
    }
  });

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

  const topRefreshBtn = document.getElementById("btn-topbar-refresh");
  if (topRefreshBtn) {
    topRefreshBtn.addEventListener("click", () => refreshAll());
  }
  const topAuditBtn = document.getElementById("btn-topbar-audit");
  if (topAuditBtn) {
    topAuditBtn.addEventListener("click", () => activateTab("audit"));
  }

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
      } else if (q.includes("tos") || q.includes("legal") || q.includes("điều khoản") || q.includes("chính sách")) {
        activateTab("legal");
      } else if (q.includes("audit") || q.includes("log") || q.includes("nhật ký")) {
        activateTab("audit");
      } else {
        activateTab("sessions", {
          afterUnlock: () => sessionsTab.setQueryAndSearch(raw),
        });
      }
    });
  }
}

async function initDashboard() {
  initThemeSwitcher();
  bindShellControls();
  stepUp.bindEvents();
  statusTab.bindEvents();
  askConfigTab.bindEvents();
  presenceCard.bindEvents();
  tunnelCard.bindEvents();
  promptConfigTab.bindEvents();
  modelsTab.bindEvents();
  permissionsTab.bindEvents();
  sessionsTab.bindEvents();
  auditTab.bindEvents();

  applyInitialCardEntrance();

  const callbackOutcome = stepUp.handleCallbackQueryParams();
  const rawInitialHash = window.location.hash.replace(/^#/, "");
  const initialTab = rawInitialHash ? normalizeTabName(rawInitialHash) : "status";

  if (callbackOutcome.status === "ok" && SENSITIVE_TABS.has(initialTab)) {
    activateTab(initialTab, { skipAutoLoad: true, suppressModal: true, updateHash: initialTab !== rawInitialHash });
    const unlockedNow = await stepUp.syncStatusFromServer(initialTab);
    if (!unlockedNow) {
      stepUp.openModal(initialTab);
    }
  } else if (callbackOutcome.status === "error") {
    const target = SENSITIVE_TABS.has(initialTab) ? initialTab : "perms";
    showToast(callbackOutcome.errorMessage, false);
    activateTab(target, { updateHash: target !== rawInitialHash, stepUpError: callbackOutcome.errorMessage });
  } else if (initialTab !== "status") {
    activateTab(initialTab, { updateHash: initialTab !== rawInitialHash });
  }

  statusTab.loadStatus(false, { silent: false });
  loadConfig();
  startBackgroundPolling({ scheduleInitialGemini: true });
  initWebTosConsentGate();
}

initDashboard();
