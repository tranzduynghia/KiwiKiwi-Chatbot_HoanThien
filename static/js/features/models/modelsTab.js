/**
 * Bộ điều khiển Tab 4: Quản lý Model Gemini, chuỗi Auto Chain và bảng ghi đè theo từng model.
 * Sử dụng ủy quyền sự kiện (event delegation) thay cho hàm toàn cục trên window.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";

const DEFAULT_FALLBACK_MODELS = ["gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-4-hydrogen"];

export function createModelsTabController({ onRefreshAll }) {
  let currentConfig = null;
  let currentModelsRuntime = null;
  let autoChainState = [];
  let overridesState = {};
  let initialState = null;

  function splitCsv(raw) {
    return String(raw || "")
      .split(/[,\n]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function readFormState() {
    return {
      models: {
        default_text_model: document.getElementById("mod-default-text").value.trim(),
        auto_model_chain: [...autoChainState],
        auto_last_model: document.getElementById("mod-auto-last").value.trim(),
        fallback_text: [...(currentConfig?.models?.fallback_text || DEFAULT_FALLBACK_MODELS)],
        hidden_models: splitCsv(document.getElementById("mod-hidden-patterns").value),
        hide_pro_models: document.getElementById("mod-hide-pro").checked,
        extra_models: splitCsv(document.getElementById("mod-extra").value),
        model_refresh_hours: Number(document.getElementById("mod-refresh-hours").value),
        overrides: JSON.parse(JSON.stringify(overridesState)),
      },
    };
  }

  function setOverride(modelId, key, value) {
    if (!overridesState[modelId]) {
      overridesState[modelId] = {};
    }
    if (key === "hidden") {
      overridesState[modelId].hidden = Boolean(value);
    } else {
      const cleaned = String(value || "").trim();
      if (cleaned) overridesState[modelId][key] = cleaned;
      else delete overridesState[modelId][key];
    }
    if (key === "tag") {
      if (value === "🆓") overridesState[modelId].is_free = true;
      else if (value === "💳") overridesState[modelId].is_free = false;
      else delete overridesState[modelId].is_free;
    }
  }

  function addToAutoChain(modelId) {
    if (!modelId || autoChainState.includes(modelId)) return;
    autoChainState.push(modelId);
    renderAutoChain();
  }

  function moveAutoChain(index, direction) {
    const target = index + direction;
    if (target < 0 || target >= autoChainState.length) return;
    [autoChainState[index], autoChainState[target]] = [autoChainState[target], autoChainState[index]];
    renderAutoChain();
  }

  function removeAutoChain(index) {
    autoChainState.splice(index, 1);
    renderAutoChain();
  }

  function renderAutoChain() {
    const listBox = document.getElementById("auto-chain-list");
    const selectEl = document.getElementById("auto-chain-select");
    const allModels = currentModelsRuntime?.all_models || [];

    selectEl.innerHTML =
      '<option value="">-- Chọn model từ danh sách --</option>' +
      allModels.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.badge)} ${escapeHtml(item.id)}</option>`).join("");

    if (!autoChainState.length) {
      const effectiveChain = (currentModelsRuntime?.auto_chain || []).join(" → ");
      listBox.innerHTML = `<div class="ox-diff-before-box text-xs rounded-lg p-3">
        Đang dùng chế độ tự chọn mặc định: <span class="font-mono text-indigo-300">${escapeHtml(effectiveChain)}</span>
      </div>`;
      return;
    }

    listBox.innerHTML = autoChainState
      .map(
        (mid, index) => `
          <div class="ox-diff-before-box flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-xs">
            <div class="flex items-center gap-2">
              <span class="w-5 h-5 rounded-full bg-indigo-500/20 text-indigo-300 flex items-center justify-center font-mono">${index + 1}</span>
              <span class="font-mono text-slate-100">${escapeHtml(mid)}</span>
            </div>
            <div class="flex items-center gap-1">
              <button type="button" data-action="move-up" data-index="${index}" class="btn-ghost px-2 py-1">↑</button>
              <button type="button" data-action="move-down" data-index="${index}" class="btn-ghost px-2 py-1">↓</button>
              <button type="button" data-action="remove" data-index="${index}" class="btn-danger px-2 py-1">×</button>
            </div>
          </div>
        `
      )
      .join("");
  }

  function renderModelsTable() {
    const query = (document.getElementById("mod-search").value || "").trim().toLowerCase();
    const tbody = document.getElementById("models-tbody");
    const filteredModels = (currentModelsRuntime?.all_models || []).filter(
      (item) => !query || item.id.toLowerCase().includes(query) || (item.display_name || "").toLowerCase().includes(query)
    );

    tbody.innerHTML = filteredModels
      .map((item) => {
        const ov = overridesState[item.id] || {};
        const displayName = ov.display_name ?? (item.display_name !== item.api_display ? item.display_name : "");
        const tag = ov.tag ?? (ov.is_free === true ? "🆓" : ov.is_free === false ? "💳" : "");
        const isHidden = ov.hidden !== undefined ? Boolean(ov.hidden) : Boolean(item.hidden);
        const isCustomTag = tag && !["🆓", "🧪", "💳"].includes(tag);

        return `
          <tr data-model-id="${escapeHtml(item.id)}" class="ox-diff-row">
            <td class="py-2.5 pr-3">
              <div class="font-mono text-slate-100">${escapeHtml(item.id)}</div>
              <div class="text-[11px] text-slate-500">Mặc định: ${escapeHtml(item.default_badge)} · ${escapeHtml(item.source)}</div>
            </td>
            <td class="py-2.5 pr-3">
              <input type="text" data-field="display_name" value="${escapeHtml(displayName)}" placeholder="${escapeHtml(item.api_display)}"
                class="inp py-1.5 text-xs" />
            </td>
            <td class="py-2.5 pr-3">
              <div class="flex gap-1">
                <select data-field="tag_preset" class="inp py-1.5 text-xs min-w-[155px] w-auto">
                  <option value="" ${!tag ? "selected" : ""}>Tự động (${escapeHtml(item.default_badge)})</option>
                  <option value="🆓" ${tag === "🆓" ? "selected" : ""}>🆓 Free</option>
                  <option value="🧪" ${tag === "🧪" ? "selected" : ""}>🧪 Preview</option>
                  <option value="💳" ${tag === "💳" ? "selected" : ""}>💳 Paid</option>
                  <option value="__custom" ${isCustomTag ? "selected" : ""}>Tùy chỉnh</option>
                </select>
                <input type="text" data-field="tag" value="${escapeHtml(tag)}" placeholder="Tag"
                  class="inp py-1.5 text-xs w-16 text-center" />
              </div>
            </td>
            <td class="py-2.5 pr-3 text-center">
              <input type="checkbox" data-field="hidden" ${isHidden ? "checked" : ""}
                class="accent-rose-500 w-4 h-4 cursor-pointer" />
            </td>
            <td class="py-2.5 text-right">
              <button type="button" data-action="add-auto" class="btn-ghost text-[11px]">+ Auto</button>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function fill(config, modelsRuntime) {
    currentConfig = config;
    currentModelsRuntime = modelsRuntime;
    const models = config.models || {};
    autoChainState = [...(models.auto_model_chain || [])];
    overridesState = JSON.parse(JSON.stringify(models.overrides || {}));

    document.getElementById("mod-default-text").value = models.default_text_model || "";
    document.getElementById("mod-auto-last").value = models.auto_last_model || "";
    document.getElementById("mod-refresh-hours").value = models.model_refresh_hours ?? 6;
    document.getElementById("mod-hide-pro").checked = models.hide_pro_models !== false;
    document.getElementById("mod-extra").value = (models.extra_models || []).join(", ");
    document.getElementById("mod-hidden-patterns").value = (models.hidden_models || []).join(", ");

    renderAutoChain();
    renderModelsTable();
    initialState = readFormState();
  }

  async function refreshModelsFromApi() {
    try {
      showToast("Đang tải lại danh sách model từ Gemini API...");
      await apiRequest("/api/models/refresh", { method: "POST" });
      await onRefreshAll();
      showToast("✅ Đã cập nhật danh sách model từ Gemini API!");
    } catch (err) {
      showToast(err.message, false);
    }
  }

  function bindEvents() {
    // Ủy quyền sự kiện cho danh sách chuỗi Auto Chain
    document.getElementById("auto-chain-list").addEventListener("click", (event) => {
      const btn = event.target.closest("button[data-action]");
      if (!btn) return;
      const index = Number(btn.dataset.index);
      if (btn.dataset.action === "move-up") moveAutoChain(index, -1);
      else if (btn.dataset.action === "move-down") moveAutoChain(index, 1);
      else if (btn.dataset.action === "remove") removeAutoChain(index);
    });

    // Ủy quyền sự kiện cho bảng ghi đè từng model (bắt cả `input` khi gõ trực tiếp lẫn `change` khi đổi checkbox/dropdown)
    const modelsTbody = document.getElementById("models-tbody");
    modelsTbody.addEventListener("input", (event) => {
      const row = event.target.closest("tr[data-model-id]");
      if (!row) return;
      const modelId = row.dataset.modelId;
      const field = event.target.dataset.field;
      if (field === "display_name") {
        setOverride(modelId, "display_name", event.target.value);
      } else if (field === "tag") {
        setOverride(modelId, "tag", event.target.value);
      }
    });

    modelsTbody.addEventListener("change", (event) => {
      const row = event.target.closest("tr[data-model-id]");
      if (!row) return;
      const modelId = row.dataset.modelId;
      const field = event.target.dataset.field;
      if (field === "display_name") {
        setOverride(modelId, "display_name", event.target.value);
      } else if (field === "tag") {
        setOverride(modelId, "tag", event.target.value);
      } else if (field === "hidden") {
        setOverride(modelId, "hidden", event.target.checked);
      } else if (field === "tag_preset") {
        const tagInput = event.target.closest("td")?.querySelector('input[data-field="tag"]');
        if (!tagInput) return;
        if (event.target.value === "__custom") {
          tagInput.focus();
          return;
        }
        tagInput.value = event.target.value;
        setOverride(modelId, "tag", event.target.value);
      }
    });

    modelsTbody.addEventListener("click", (event) => {
      const btn = event.target.closest('button[data-action="add-auto"]');
      if (!btn) return;
      const row = btn.closest("tr[data-model-id]");
      if (row) addToAutoChain(row.dataset.modelId);
    });

    document.getElementById("btn-add-auto-chain").addEventListener("click", () => {
      const customInput = document.getElementById("auto-chain-custom");
      const chosen = customInput.value.trim() || document.getElementById("auto-chain-select").value;
      if (!chosen) return;
      addToAutoChain(chosen);
      customInput.value = "";
    });

    document.getElementById("btn-clear-auto-chain").addEventListener("click", () => {
      autoChainState = [];
      renderAutoChain();
    });

    document.getElementById("mod-search").addEventListener("input", renderModelsTable);
    document.getElementById("btn-refresh-models-tab").addEventListener("click", refreshModelsFromApi);
    document.getElementById("btn-refresh-models-st").addEventListener("click", refreshModelsFromApi);

    document.getElementById("btn-save-models").addEventListener("click", async () => {
      const nextState = readFormState();
      await confirmSaveChanges({
        title: "Xác nhận lưu cấu hình Model Gemini",
        subtitle: "Chuỗi Auto Chain, danh sách model ẩn và ghi đè hiển thị sẽ áp dụng ngay",
        before: initialState || {},
        after: nextState,
        onConfirm: async (changes) => {
          await apiRequest("/api/config/models", {
            method: "PUT",
            body: JSON.stringify({ ...nextState.models, changes }),
          });
          showToast("✅ Đã lưu cấu hình Model Gemini (áp dụng ngay)!");
          await onRefreshAll();
        },
      });
    });
  }

  return { fill, bindEvents };
}
