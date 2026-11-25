/**
 * Bộ điều khiển Tab 3: Chỉnh sửa, khôi phục mặc định và lưu System Instruction / OCR Instruction.
 */

import { apiRequest, showToast } from "../../core/api.js";

export function createPromptConfigTabController({
  defaultSystemPrompt,
  defaultOcrInstruction,
  onConfigSaved,
}) {
  function updateCharCount() {
    const length = document.getElementById("inp-system-prompt").value.length;
    document.getElementById("prompt-char-count").textContent = `${length} ký tự`;
  }

  function fill(config) {
    document.getElementById("inp-system-prompt").value = config.system_prompt || "";
    document.getElementById("inp-ocr-prompt").value = config.ocr_instruction || "";
    document.getElementById("inp-append-date").checked = config.append_current_date !== false;
    updateCharCount();
  }

  function bindEvents() {
    document.getElementById("inp-system-prompt").addEventListener("input", updateCharCount);

    document.getElementById("btn-reset-sys-prompt").addEventListener("click", () => {
      document.getElementById("inp-system-prompt").value = defaultSystemPrompt;
      updateCharCount();
    });

    document.getElementById("btn-reset-ocr-prompt").addEventListener("click", () => {
      document.getElementById("inp-ocr-prompt").value = defaultOcrInstruction;
    });

    document.getElementById("btn-save-prompt").addEventListener("click", async () => {
      try {
        await apiRequest("/api/config/prompt", {
          method: "PUT",
          body: JSON.stringify({
            system_prompt: document.getElementById("inp-system-prompt").value,
            ocr_instruction: document.getElementById("inp-ocr-prompt").value,
            append_current_date: document.getElementById("inp-append-date").checked,
          }),
        });
        showToast("✅ Đã lưu System Instruction (áp dụng ngay)!");
        await onConfigSaved();
      } catch (err) {
        showToast(err.message, false);
      }
    });
  }

  return { fill, bindEvents };
}
