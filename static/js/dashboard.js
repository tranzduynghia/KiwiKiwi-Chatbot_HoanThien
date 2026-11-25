/**
 * Điểm vào điều phối toàn bộ các tab tính năng trên Web Dashboard.
 */

import { apiRequest, showToast } from "./core/api.js";
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

let askConfigTab;
let promptConfigTab;
let modelsTab;

async function loadConfig() {
  try {
    const data = await apiRequest("/api/config");
    const cfg = data.config;
    document.getElementById("hdr-updated").textContent =
      `Cập nhật lúc: ${new Date(cfg.updated_at).toLocaleString("vi-VN")} bởi ${cfg.updated_by || "system"}`;

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

// Điều hướng chuyển đổi giữa các tab trên thanh tiêu đề
function bindTabNavigation() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetTab = btn.dataset.tab;
      document.querySelectorAll(".tab-btn").forEach((item) => {
        item.dataset.active = String(item === btn);
      });
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("hidden", panel.id !== `tab-${targetTab}`);
      });
      if (targetTab === "perms") {
        permissionsTab.loadPermissions();
      }
    });
  });
}

// Khởi chạy toàn bộ trình lắng nghe sự kiện và nạp dữ liệu ban đầu
bindTabNavigation();
statusTab.bindEvents();
askConfigTab.bindEvents();
promptConfigTab.bindEvents();
modelsTab.bindEvents();
permissionsTab.bindEvents();

statusTab.loadStatus(false);
loadConfig();
statusTab.startAutoRefresh();
