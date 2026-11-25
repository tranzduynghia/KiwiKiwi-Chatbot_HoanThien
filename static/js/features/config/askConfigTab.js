/**
 * Bộ điều khiển Tab 2: Điền dữ liệu và lưu cấu hình lệnh /ask cùng cấu hình phiên chat.
 */

import { apiRequest, showToast } from "../../core/api.js";

export function createAskConfigTabController({ onConfigSaved }) {
  function fill(config) {
    const ask = config.ask || {};
    const session = config.session || {};

    document.getElementById("ask-cooldown").value = ask.user_cooldown ?? 5;
    document.getElementById("ask-concurrent").value = ask.max_concurrent ?? 2;
    document.getElementById("ask-max-file-mb").value = ask.max_file_mb ?? 10;
    document.getElementById("ask-timeout").value = ask.gemini_timeout_seconds ?? 170;
    document.getElementById("ask-retries").value = ask.max_retries ?? 3;
    document.getElementById("ask-default-thinking").value = ask.default_thinking_level || "";
    document.getElementById("ask-chunk-limit").value = ask.chunk_char_limit ?? 1900;
    document.getElementById("ask-max-chunks").value = ask.max_reply_chunks ?? 6;
    document.getElementById("ask-tb-low").value = ask.thinking_budgets?.low ?? 1024;
    document.getElementById("ask-tb-medium").value = ask.thinking_budgets?.medium ?? 8192;
    document.getElementById("ask-tb-high").value = ask.thinking_budgets?.high ?? 24576;
    document.getElementById("ask-allow-file").checked = ask.allow_file !== false;
    document.getElementById("ask-allow-ocr").checked = ask.allow_ocr !== false;
    document.getElementById("ask-allow-web").checked = ask.allow_web_search !== false;
    document.getElementById("ask-allow-thinking").checked = ask.allow_thinking !== false;
    document.getElementById("ask-default-web").checked = Boolean(ask.default_web_search);
    document.getElementById("ask-show-thoughts").checked = ask.show_thoughts_embed !== false;
    document.getElementById("ask-show-sources").checked = ask.show_sources !== false;

    document.getElementById("sess-idle-hours").value = session.session_idle_hours ?? 2;
    document.getElementById("sess-max-msgs").value = session.max_history_messages ?? 20;
    document.getElementById("sess-max-chars").value = session.max_history_chars ?? 24000;
    document.getElementById("sess-stored-chars").value = session.max_stored_chars ?? 4000;
    document.getElementById("sess-max-turns").value = session.session_max_turns ?? 500;
    document.getElementById("sess-sweep-mins").value = session.sweep_minutes ?? 5;
    document.getElementById("sess-quota-mb").value = session.mongodb_quota_mb ?? 512;
    document.getElementById("sess-ram-limit").value = session.ram_limit_mb ?? "";
  }

  function bindEvents() {
    document.getElementById("btn-save-ask").addEventListener("click", async () => {
      try {
        const ramVal = document.getElementById("sess-ram-limit").value.trim();
        const payload = {
          ask: {
            user_cooldown: Number(document.getElementById("ask-cooldown").value),
            max_concurrent: Number(document.getElementById("ask-concurrent").value),
            max_file_mb: Number(document.getElementById("ask-max-file-mb").value),
            gemini_timeout_seconds: Number(document.getElementById("ask-timeout").value),
            max_retries: Number(document.getElementById("ask-retries").value),
            default_thinking_level: document.getElementById("ask-default-thinking").value,
            chunk_char_limit: Number(document.getElementById("ask-chunk-limit").value),
            max_reply_chunks: Number(document.getElementById("ask-max-chunks").value),
            thinking_budgets: {
              low: Number(document.getElementById("ask-tb-low").value),
              medium: Number(document.getElementById("ask-tb-medium").value),
              high: Number(document.getElementById("ask-tb-high").value),
            },
            allow_file: document.getElementById("ask-allow-file").checked,
            allow_ocr: document.getElementById("ask-allow-ocr").checked,
            allow_web_search: document.getElementById("ask-allow-web").checked,
            allow_thinking: document.getElementById("ask-allow-thinking").checked,
            default_web_search: document.getElementById("ask-default-web").checked,
            show_thoughts_embed: document.getElementById("ask-show-thoughts").checked,
            show_sources: document.getElementById("ask-show-sources").checked,
          },
          session: {
            session_idle_hours: Number(document.getElementById("sess-idle-hours").value),
            max_history_messages: Number(document.getElementById("sess-max-msgs").value),
            max_history_chars: Number(document.getElementById("sess-max-chars").value),
            max_stored_chars: Number(document.getElementById("sess-stored-chars").value),
            session_max_turns: Number(document.getElementById("sess-max-turns").value),
            sweep_minutes: Number(document.getElementById("sess-sweep-mins").value),
            mongodb_quota_mb: Number(document.getElementById("sess-quota-mb").value),
            ram_limit_mb: ramVal ? Number(ramVal) : null,
          },
        };
        await apiRequest("/api/config/ask", { method: "PUT", body: JSON.stringify(payload) });
        showToast("✅ Đã lưu cấu hình /ask & phiên chat (áp dụng ngay)!");
        await onConfigSaved();
      } catch (err) {
        showToast(err.message, false);
      }
    });
  }

  return { fill, bindEvents };
}
