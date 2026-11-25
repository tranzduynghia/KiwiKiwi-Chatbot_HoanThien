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

// Kích hoạt tab theo mã tên và đóng ngăn kéo sidebar trên màn hình nhỏ
function activateTab(targetTab) {
  document.querySelectorAll(".tab-btn").forEach((item) => {
    item.dataset.active = String(item.dataset.tab === targetTab);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `tab-${targetTab}`);
  });
  document.body.classList.remove("sidebar-mobile-open");

  if (targetTab === "perms") {
    permissionsTab.loadPermissions();
  } else if (targetTab === "audit") {
    auditTab.loadAuditLogs();
  }
}

function bindShellControls() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
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

  // Ô tìm kiếm nhanh trên Topbar: nhấn Enter để lọc model hoặc nhảy tới tab tương ứng
  const globalSearch = document.getElementById("global-search");
  if (globalSearch) {
    globalSearch.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      const q = globalSearch.value.trim().toLowerCase();
      if (!q) return;
      if (q.includes("gemini") || q.includes("model") || q.includes("flash") || q.includes("pro")) {
        activateTab("models");
        const modSearch = document.getElementById("mod-search");
        if (modSearch) {
          modSearch.value = globalSearch.value.trim();
          modSearch.dispatchEvent(new Event("input"));
        }
      } else if (q.includes("prompt") || q.includes("ocr") || q.includes("system")) {
        activateTab("prompt");
      } else if (q.includes("ask") || q.includes("cooldown") || q.includes("session")) {
        activateTab("ask");
      } else if (q.includes("perm") || q.includes("admin") || q.includes("user") || q.includes("quyền")) {
        activateTab("perms");
      } else if (q.includes("audit") || q.includes("log") || q.includes("nhật ký")) {
        activateTab("audit");
      } else {
        activateTab("status");
      }
    });
  }
}

// Khởi chạy toàn bộ trình lắng nghe sự kiện và nạp dữ liệu ban đầu
bindShellControls();
statusTab.bindEvents();
askConfigTab.bindEvents();
promptConfigTab.bindEvents();
modelsTab.bindEvents();
permissionsTab.bindEvents();
auditTab.bindEvents();

statusTab.loadStatus(false);
loadConfig();
statusTab.startAutoRefresh();
