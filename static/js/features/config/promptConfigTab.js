/**
 * Bộ điều khiển Tab 3: Chỉnh sửa, khôi phục mặc định và lưu System Instruction / OCR Instruction.
 */

import { apiRequest, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";

export function createPromptConfigTabController({
  defaultSystemPrompt,
  defaultOcrInstruction,
  onConfigSaved,
}) {
  let initialState = null;

  function readFormState() {
    return {
      system_prompt: document.getElementById("inp-system-prompt").value.trim(),
      ocr_instruction: document.getElementById("inp-ocr-prompt").value.trim(),
      append_current_date: document.getElementById("inp-append-date").checked,
    };
  }

  function updateCharCount() {
    const length = document.getElementById("inp-system-prompt").value.length;
    document.getElementById("prompt-char-count").textContent = `${length} ký tự`;
  }

  function fill(config) {
    document.getElementById("inp-system-prompt").value = config.system_prompt || "";
    document.getElementById("inp-ocr-prompt").value = config.ocr_instruction || "";
    document.getElementById("inp-append-date").checked = config.append_current_date !== false;
    updateCharCount();
    initialState = readFormState();
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
      const nextState = readFormState();
      await confirmSaveChanges({
        title: "Xác nhận lưu System Instruction & OCR",
        subtitle: "Chỉ thị hệ thống mới sẽ áp dụng ngay cho các câu hỏi /ask tiếp theo",
        before: initialState || {},
        after: nextState,
        onConfirm: async (changes) => {
          await apiRequest("/api/config/prompt", {
            method: "PUT",
            body: JSON.stringify({ ...nextState, changes }),
          });
          showToast("✅ Đã lưu System Instruction (áp dụng ngay)!");
          await onConfigSaved();
        },
      });
    });
  }

  return { fill, bindEvents };
}
