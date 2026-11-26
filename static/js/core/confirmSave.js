/**
 * Hệ thống Hộp thoại Xác nhận trong ứng dụng (`showConfirmModal` & `confirmSaveChanges`):
 * - Thay thế hoàn toàn mọi hộp thoại trình duyệt `window.confirm()` / `alert()` / `prompt()`
 * - Tính toán chính xác diff từng trường giữa trạng thái tải ban đầu (`before`) và trạng thái biểu mẫu (`after`)
 * - Hỗ trợ xem trước có thanh cuộn + đếm ký tự/dòng cho System Instruction & OCR Instruction
 * - Hiển thị nhãn rõ ràng cho Model Gemini, chuỗi Auto Chain, ghi đè model, Tunnel, Watchdog & Phân quyền
 * - Che giá trị nhạy cảm dạng tiền tố (`eyJh•••••`), hiển thị biểu ngữ cảnh báo vàng trong Modal
 * - Gửi kèm đối tượng `{ field: { old, new } }` cho Backend Audit Log trên mọi lượt lưu
 */

import { escapeHtml, showToast } from "./api.js";

const SENSITIVE_KEY_TOKENS = [
  "password",
  "pepper",
  "secret",
  "token",
  "api_key",
  "apikey",
  "uri",
  "salt",
  "hash",
  "signing_key",
  "private_key",
  "credential",
];
const SAFE_NON_SECRET_KEYS = new Set([
  "has_cf_tunnel_token",
  "cf_tunnel_has_token",
  "outbound_proxy_configured",
  "discord_oauth_enabled",
]);
const STATIC_SECRET_PLACEHOLDER = "••••••••••••";
const LONG_TEXT_FIELD_KEYS = new Set(["system_prompt", "ocr_instruction"]);
const MAX_PREVIEW_CHARS = 2500;

const DEFAULT_FIELD_LABELS = {
  // /ask & phiên chat
  "ask.user_cooldown": "Cooldown giữa 2 lệnh /ask",
  "ask.max_concurrent": "Số request Gemini song song tối đa",
  "ask.max_file_mb": "Dung lượng tệp đính kèm tối đa",
  "ask.gemini_timeout_seconds": "Timeout gọi Gemini API",
  "ask.max_retries": "Số lần thử lại tối đa",
  "ask.default_thinking_level": "Mức Thinking mặc định",
  "ask.chunk_char_limit": "Giới hạn ký tự mỗi tin phản hồi",
  "ask.max_reply_chunks": "Số tin nhắn tách tối đa",
  "ask.thinking_budgets.low": "Thinking Budget (Low)",
  "ask.thinking_budgets.medium": "Thinking Budget (Medium)",
  "ask.thinking_budgets.high": "Thinking Budget (High)",
  "ask.allow_file": "Cho phép đính kèm tệp",
  "ask.allow_ocr": "Cho phép OCR trích xuất văn bản",
  "ask.allow_web_search": "Cho phép tìm kiếm Web",
  "ask.allow_thinking": "Cho phép chế độ Thinking",
  "ask.default_web_search": "Bật Web Search mặc định",
  "ask.show_thoughts_embed": "Hiển thị Embed suy nghĩ (Thoughts)",
  "ask.show_sources": "Hiển thị nguồn trích dẫn",
  "session.session_idle_hours": "Thời gian hết hạn phiên nghỉ",
  "session.max_history_messages": "Số tin nhắn lịch sử tối đa",
  "session.max_history_chars": "Giới hạn ký tự lịch sử gửi AI",
  "session.max_stored_chars": "Giới hạn ký tự lưu mỗi tin",
  "session.session_max_turns": "Số lượt tối đa mỗi phiên",
  "session.sweep_minutes": "Chu kỳ quét lưu trữ phiên",
  "session.mongodb_quota_mb": "Hạn mức lưu trữ MongoDB Atlas",
  "session.ram_limit_mb": "Giới hạn RAM Container (ghi đè)",
  "watchdog_trigger_mb": "Ngưỡng kích hoạt Memory Watchdog",
  // Prompt
  "system_prompt": "System Instruction",
  "ocr_instruction": "OCR Instruction",
  "append_current_date": "Tự động chèn ngày hiện tại",
  // Models
  "models.default_text_model": "Model AI mặc định (Default)",
  "models.auto_model_chain": "Chuỗi Auto Model Chain",
  "models.auto_last_model": "Model dự phòng cuối chuỗi Auto",
  "models.fallback_text": "Danh sách Fallback Model",
  "models.hidden_models": "Mẫu wildcard model bị ẩn",
  "models.hide_pro_models": "Ẩn toàn bộ model hạng Pro",
  "models.extra_models": "Danh sách model bổ sung thủ công",
  "models.model_refresh_hours": "Chu kỳ tự tải lại danh sách model",
  // Presence
  "presence.status": "Trạng thái trực tuyến Bot",
  "presence.custom_status": "Custom Status (Bong bóng chữ)",
  "presence.activity_type": "Loại hoạt động chính",
  "presence.activity_text": "Nội dung hoạt động chính",
  "presence.auto_rotate": "Luân phiên tự động (mỗi 20s)",
  // Cloudflare Tunnel
  "tunnel.cf_tunnel_enabled": "Trạng thái Cloudflare Tunnel",
  "tunnel.cf_tunnel_mode": "Chế độ Cloudflare Tunnel",
  "tunnel.cf_tunnel_token": "Cloudflare Tunnel Token",
  "tunnel.tunnel_only_inbound": "Chỉ nhận truy cập qua Cloudflare Tunnel",
  "tunnel.proxy_headers": "Tin cậy Proxy Headers (X-Forwarded-For / Proto)",
  // Permissions & Dashboard Users
  "user_id": "Discord User ID",
  "label": "Nhãn ghi chú",
  "is_admin": "Quyền Quản trị viên (Admin)",
  "can_dashboard": "Quyền truy cập Dashboard",
  "can_status": "Quyền dùng lệnh /status",
  "username": "Tên đăng nhập Dashboard",
  "role": "Vai trò tài khoản",
  "password": "Mật khẩu đăng nhập",
};

const MODEL_OVERRIDE_PROP_LABELS = {
  display_name: "Tên hiển thị",
  tag: "Tag phân loại",
  hidden: "Ẩn khỏi /ask",
  is_free: "Gắn nhãn Free",
};

const DEFAULT_FIELD_UNITS = {
  "ask.user_cooldown": "s",
  "ask.max_file_mb": " MB",
  "ask.gemini_timeout_seconds": "s",
  "session.session_idle_hours": " giờ",
  "session.sweep_minutes": " phút",
  "session.mongodb_quota_mb": " MB",
  "session.ram_limit_mb": " MB",
  "watchdog_trigger_mb": " MB",
  "models.model_refresh_hours": " giờ",
};

export function isSensitiveField(key) {
  const lowered = String(key || "").trim().toLowerCase();
  if (!lowered) return false;
  const leaf = lowered.split(".").pop();
  if (SAFE_NON_SECRET_KEYS.has(leaf)) return false;
  return SENSITIVE_KEY_TOKENS.some((tok) => lowered.includes(tok));
}

export function maskSecretPreview(value) {
  if (value === null || value === undefined || value === "") return "(trống)";
  const text = String(value).trim();
  if (!text || text === "(trống)") return "(trống)";
  return STATIC_SECRET_PLACEHOLDER;
}

function isPlainObject(val) {
  return Boolean(val) && typeof val === "object" && !Array.isArray(val);
}

function deepEqual(a, b) {
  if (a === b) return true;
  if (Number.isNaN(a) && Number.isNaN(b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    return a.every((item, idx) => deepEqual(item, b[idx]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const keysA = Object.keys(a);
    const keysB = Object.keys(b);
    if (keysA.length !== keysB.length) return false;
    return keysA.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]));
  }
  return false;
}

function resolveFieldLabel(key, customLabels = {}) {
  if (customLabels[key]) return customLabels[key];
  if (DEFAULT_FIELD_LABELS[key]) return DEFAULT_FIELD_LABELS[key];
  const overrideMatch = String(key || "").match(/^models\.overrides\.(.+)\.([^.]+)$/);
  if (overrideMatch) {
    const [, modelId, prop] = overrideMatch;
    const propLabel = MODEL_OVERRIDE_PROP_LABELS[prop] || prop;
    return `Model ${modelId} · ${propLabel}`;
  }
  return key;
}

function flattenEntries(beforeObj, afterObj, prefix = "", out = []) {
  const keys = Array.from(
    new Set([
      ...(isPlainObject(beforeObj) ? Object.keys(beforeObj) : []),
      ...(isPlainObject(afterObj) ? Object.keys(afterObj) : []),
    ])
  );

  for (const key of keys) {
    const path = prefix ? `${prefix}.${key}` : key;
    const oldVal = isPlainObject(beforeObj) ? beforeObj[key] : undefined;
    const newVal = isPlainObject(afterObj) ? afterObj[key] : undefined;

    if (deepEqual(oldVal, newVal)) continue;

    if (isPlainObject(oldVal) && isPlainObject(newVal)) {
      flattenEntries(oldVal, newVal, path, out);
    } else {
      out.push({ key: path, before: oldVal, after: newVal });
    }
  }
  return out;
}

function countLines(text) {
  if (!text) return 0;
  return String(text).split(/\r?\n/).length;
}

function formatNumber(num) {
  return Number(num || 0).toLocaleString("en-US");
}

function isLongTextEntry(key, beforeVal, afterVal, isSecret) {
  if (isSecret) return false;
  if (LONG_TEXT_FIELD_KEYS.has(key)) return true;
  const oldStr = typeof beforeVal === "string" ? beforeVal : "";
  const newStr = typeof afterVal === "string" ? afterVal : "";
  return (
    oldStr.length > 120 ||
    newStr.length > 120 ||
    oldStr.includes("\n") ||
    newStr.includes("\n")
  );
}

function truncatePreviewText(text) {
  const str = String(text ?? "");
  if (!str) return "(trống)";
  if (str.length <= MAX_PREVIEW_CHARS) return str;
  return `${str.slice(0, MAX_PREVIEW_CHARS)}\n… (đã rút gọn hiển thị, tổng ${formatNumber(str.length)} ký tự)`;
}

function formatDisplayValue(key, value, { units = {}, isSecret = false } = {}) {
  if (isSecret) return maskSecretPreview(value);
  if (key === "models.default_text_model" && (!value || value === "")) {
    return "(Tự động / Auto Chain)";
  }
  if (value === null || value === undefined || value === "") return "(trống)";
  if (typeof value === "boolean") return value ? "Bật" : "Tắt";
  if (Array.isArray(value)) {
    if (!value.length) return "(trống)";
    if (key === "models.auto_model_chain" || key === "models.fallback_text") {
      return value.join(" → ");
    }
    return value.join(", ");
  }
  if (isPlainObject(value)) {
    const raw = JSON.stringify(value);
    return raw.length > 180 ? `${raw.slice(0, 180)}…` : raw;
  }
  if (typeof value === "number") {
    const unit = units[key] ?? DEFAULT_FIELD_UNITS[key] ?? "";
    return `${value}${unit}`;
  }
  const text = String(value);
  return text.length > 180 ? `${text.slice(0, 180)}… (${formatNumber(text.length)} ký tự)` : text;
}

function detectHighImpactWarning(key, beforeVal, afterVal) {
  const k = String(key || "");
  if ((k === "tunnel.tunnel_only_inbound" || k === "tunnel_only_inbound") && afterVal === true) {
    return "Bật 'Chỉ nhận truy cập qua Cloudflare Tunnel' sẽ chặn toàn bộ truy cập trực tiếp (kể cả tên miền *.code.run) nếu không có header Cloudflare (Cf-Ray / CF-Connecting-IP). Đảm bảo Tunnel đang hoạt động trước khi xác nhận!";
  }
  if (k === "tunnel.cf_tunnel_token" || k === "cf_tunnel_token") {
    return "Thay đổi Cloudflare Tunnel Token sẽ khởi động lại ngay tiến trình cloudflared và có thể làm gián đoạn đường hầm nếu Token không hợp lệ.";
  }
  if ((k === "watchdog_trigger_mb" || k === "session.ram_limit_mb") && typeof afterVal === "number" && typeof beforeVal === "number" && afterVal < beforeVal) {
    return `Giảm ngưỡng bộ nhớ (${beforeVal} MB → ${afterVal} MB) sẽ kích hoạt giới hạn hoặc dọn bộ đệm RAM sớm hơn.`;
  }
  if (k === "is_admin" || (k === "role" && afterVal === "admin")) {
    return "Thay đổi quyền Quản trị viên (Admin) ảnh hưởng trực tiếp tới quyền quản lý phân quyền, Cloudflare Tunnel và cấu hình hệ thống.";
  }
  if (k === "ask.user_cooldown" && Number(afterVal) === 0) {
    return "Đặt Cooldown /ask về 0s có thể khiến người dùng spam lệnh liên tục và nhanh chạm hạn mức Gemini API.";
  }
  if (k === "ask.max_concurrent" && Number(afterVal) >= 10) {
    return `Tăng số request song song lên ${afterVal} có thể làm tăng đột biến RAM container và dễ chạm lỗi 429 Quota.`;
  }
  if (k === "ask.gemini_timeout_seconds" && Number(afterVal) < 30) {
    return `Timeout Gemini API thấp (${afterVal}s) có thể làm ngắt sớm các câu trả lời dài hoặc bật Thinking.`;
  }
  return null;
}

export function computeFormDiff(before, after, { labels = {}, units = {}, customWarnings = [] } = {}) {
  const rawDiffs = flattenEntries(before || {}, after || {});
  const entries = [];
  const changesMap = {};
  const warnings = [...customWarnings];

  for (const item of rawDiffs) {
    const secret = isSensitiveField(item.key);
    const label = resolveFieldLabel(item.key, labels);
    const warning = detectHighImpactWarning(item.key, item.before, item.after);
    if (warning && !warnings.includes(warning)) {
      warnings.push(warning);
    }

    const safeOld = secret ? maskSecretPreview(item.before) : item.before ?? null;
    const safeNew = secret ? maskSecretPreview(item.after) : item.after ?? null;
    changesMap[item.key] = { old: safeOld, new: safeNew };

    const isLongText = isLongTextEntry(item.key, item.before, item.after, secret);
    entries.push({
      key: item.key,
      label,
      before: item.before,
      after: item.after,
      formattedBefore: formatDisplayValue(item.key, item.before, { units, isSecret: secret }),
      formattedAfter: formatDisplayValue(item.key, item.after, { units, isSecret: secret }),
      isSensitive: secret,
      isHighImpact: Boolean(warning),
      isLongText,
      warning,
    });
  }

  return { entries, changesMap, warnings };
}

function normalizeManualEntries(changes, { labels = {}, units = {} } = {}) {
  if (!changes) return { entries: [], changesMap: {} };
  const entries = [];
  const changesMap = {};

  if (Array.isArray(changes)) {
    for (const item of changes) {
      if (!item || typeof item !== "object") continue;
      const key = String(item.key || item.field || "action");
      const secret = item.isSensitive ?? isSensitiveField(key);
      const label = item.label || resolveFieldLabel(key, labels);
      const beforeVal = item.before ?? item.old;
      const afterVal = item.after ?? item.new;
      const safeOld = secret ? maskSecretPreview(beforeVal) : beforeVal ?? null;
      const safeNew = secret ? maskSecretPreview(afterVal) : afterVal ?? null;
      changesMap[key] = { old: safeOld, new: safeNew };
      entries.push({
        key,
        label,
        before: beforeVal,
        after: afterVal,
        formattedBefore: item.formattedBefore ?? formatDisplayValue(key, beforeVal, { units, isSecret: secret }),
        formattedAfter: item.formattedAfter ?? formatDisplayValue(key, afterVal, { units, isSecret: secret }),
        isSensitive: secret,
        isHighImpact: Boolean(item.isHighImpact),
        isLongText: isLongTextEntry(key, beforeVal, afterVal, secret),
      });
    }
    return { entries, changesMap };
  }

  if (isPlainObject(changes)) {
    for (const [key, pair] of Object.entries(changes)) {
      const secret = isSensitiveField(key);
      const label = resolveFieldLabel(key, labels);
      const beforeVal = isPlainObject(pair) ? pair.old ?? pair.before : undefined;
      const afterVal = isPlainObject(pair) ? pair.new ?? pair.after : pair;
      const safeOld = secret ? maskSecretPreview(beforeVal) : beforeVal ?? null;
      const safeNew = secret ? maskSecretPreview(afterVal) : afterVal ?? null;
      changesMap[key] = { old: safeOld, new: safeNew };
      entries.push({
        key,
        label,
        before: beforeVal,
        after: afterVal,
        formattedBefore: formatDisplayValue(key, beforeVal, { units, isSecret: secret }),
        formattedAfter: formatDisplayValue(key, afterVal, { units, isSecret: secret }),
        isSensitive: secret,
        isHighImpact: Boolean(detectHighImpactWarning(key, beforeVal, afterVal)),
        isLongText: isLongTextEntry(key, beforeVal, afterVal, secret),
      });
    }
  }
  return { entries, changesMap };
}

function renderLongTextDiffRow(row) {
  const oldStr = String(row.before ?? "");
  const newStr = String(row.after ?? "");
  const oldChars = oldStr.length;
  const newChars = newStr.length;
  const deltaChars = newChars - oldChars;
  const deltaSign = deltaChars > 0 ? `+${formatNumber(deltaChars)}` : formatNumber(deltaChars);
  const deltaClass = deltaChars >= 0 ? "text-emerald-300" : "text-amber-300";
  const oldLines = countLines(oldStr);
  const newLines = countLines(newStr);

  return `
    <tr class="align-top bg-[#0c1018]/60">
      <td colspan="3" class="p-3.5 space-y-2.5">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <span class="font-medium text-slate-100">${escapeHtml(row.label)}</span>
            <span class="ml-2 font-mono text-[10px] text-[#5e6778]">${escapeHtml(row.key)}</span>
          </div>
          <span class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-[#131720] border border-[#252b38] font-mono text-[11px] text-slate-300">
            <span>${formatNumber(oldChars)} ký tự (${oldLines} dòng)</span>
            <span class="text-[#5e6778]">→</span>
            <span class="${deltaClass} font-semibold">${formatNumber(newChars)} ký tự (${newLines} dòng)</span>
            <span class="text-[10px] ${deltaClass}">(${deltaSign})</span>
          </span>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="space-y-1">
            <div class="text-[10px] uppercase tracking-wider text-[#8b95a7] font-medium">Bản cũ (Before)</div>
            <pre class="max-h-40 overflow-y-auto rounded-lg bg-[#131720] border border-[#1e232e] p-2.5 font-mono text-[11px] text-[#8b95a7] whitespace-pre-wrap break-words leading-relaxed">${escapeHtml(truncatePreviewText(oldStr))}</pre>
          </div>
          <div class="space-y-1">
            <div class="text-[10px] uppercase tracking-wider text-emerald-400 font-medium">Bản mới (After)</div>
            <pre class="max-h-40 overflow-y-auto rounded-lg bg-emerald-950/20 border border-emerald-500/30 p-2.5 font-mono text-[11px] text-emerald-200 whitespace-pre-wrap break-words leading-relaxed">${escapeHtml(truncatePreviewText(newStr))}</pre>
          </div>
        </div>
      </td>
    </tr>
  `;
}

function renderStandardDiffRow(row) {
  const afterClass = row.isHighImpact
    ? "text-amber-300 bg-amber-500/10 border-amber-500/30"
    : "text-emerald-300 bg-emerald-500/10 border-emerald-500/25";
  const badge = row.isHighImpact
    ? '<span class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">Tác động cao</span>'
    : row.isSensitive
      ? '<span class="ml-1.5 text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">Đã che</span>'
      : "";

  return `
    <tr class="align-top hover:bg-[#131720]/70">
      <td class="py-2.5 px-3.5">
        <div class="font-medium text-slate-100 leading-snug">${escapeHtml(row.label)}${badge}</div>
        <div class="font-mono text-[10px] text-[#5e6778] mt-0.5 break-all">${escapeHtml(row.key)}</div>
      </td>
      <td class="py-2.5 px-3.5">
        <span class="inline-block max-w-full font-mono text-[11px] text-[#8b95a7] bg-[#131720] border border-[#1e232e] rounded px-2 py-1 break-words whitespace-pre-wrap">${escapeHtml(row.formattedBefore)}</span>
      </td>
      <td class="py-2.5 px-3.5">
        <span class="inline-block max-w-full font-mono text-[11px] border rounded px-2 py-1 break-words whitespace-pre-wrap ${afterClass}">${escapeHtml(row.formattedAfter)}</span>
      </td>
    </tr>
  `;
}

/**
 * Hiển thị Hộp thoại Xác nhận Dark Glass trực tiếp trong DOM (`#confirm-save-modal`).
 * Thay thế hoàn toàn `window.confirm()`, `window.alert()` và `window.prompt()`.
 */
export function showConfirmModal({
  title = "Xác nhận thao tác",
  subtitle = "Kiểm tra kỹ thông tin thay đổi trước khi áp dụng tức thì và ghi Audit Log",
  warning = "",
  warnings = [],
  changes = null,
  entries = null,
  labels = {},
  units = {},
  confirmLabel = "Xác nhận lưu",
  cancelLabel = "Hủy bỏ",
  confirmTone = "primary",
  inputField = null,
  onConfirm = null,
} = {}) {
  const manual = entries
    ? { entries, changesMap: isPlainObject(changes) ? changes : {} }
    : normalizeManualEntries(changes, { labels, units });
  const finalEntries = manual.entries;
  const changesMap = manual.changesMap;

  const mergedWarnings = [];
  if (warning) mergedWarnings.push(String(warning));
  for (const w of warnings || []) {
    if (w && !mergedWarnings.includes(String(w))) mergedWarnings.push(String(w));
  }

  const modal = document.getElementById("confirm-save-modal");
  const panel = document.getElementById("confirm-save-panel");
  const iconEl = document.getElementById("confirm-save-icon");
  const titleEl = document.getElementById("confirm-save-title");
  const subtitleEl = document.getElementById("confirm-save-subtitle");
  const warningBox = document.getElementById("confirm-save-warning");
  const warningList = document.getElementById("confirm-save-warning-list");
  const inputWrap = document.getElementById("confirm-save-input-wrap");
  const inputLabel = document.getElementById("confirm-save-input-label");
  const inputEl = document.getElementById("confirm-save-input");
  const inputErrEl = document.getElementById("confirm-save-input-error");
  const tableWrap = document.getElementById("confirm-save-table-wrap");
  const tbody = document.getElementById("confirm-save-tbody");
  const countEl = document.getElementById("confirm-save-count");
  const cancelBtn = document.getElementById("btn-cancel-confirm-save");
  const closeBtn = document.getElementById("btn-close-confirm-save");
  const submitBtn = document.getElementById("btn-submit-confirm-save");
  const spinnerEl = document.getElementById("confirm-save-spinner");
  const submitTextEl = document.getElementById("confirm-save-btn-text");

  if (!modal || !submitBtn || !cancelBtn) {
    return Promise.resolve(onConfirm ? onConfirm(changesMap, finalEntries, "") : null).then((result) => ({
      confirmed: true,
      result,
      changes: changesMap,
    }));
  }

  // Biểu tượng & màu nút theo mức độ (primary / danger / success)
  const isDanger = confirmTone === "danger";
  const isSuccess = confirmTone === "success";
  if (iconEl) {
    iconEl.textContent = isDanger ? "!" : "✓";
    iconEl.className = `inline-flex items-center justify-center w-7 h-7 rounded-lg border text-sm shrink-0 ${
      isDanger
        ? "bg-rose-500/20 border-rose-500/40 text-rose-300 font-bold"
        : isSuccess
          ? "bg-emerald-500/20 border-emerald-500/40 text-emerald-300 font-bold"
          : "bg-rose-500/15 border-rose-500/30 text-rose-300"
    }`;
  }
  submitBtn.className = `${
    isDanger
      ? "px-4 py-2 rounded-lg text-xs font-medium bg-rose-600 hover:bg-rose-500 text-white border border-rose-500/40 transition-colors"
      : isSuccess
        ? "px-4 py-2 rounded-lg text-xs font-medium bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-500/40 transition-colors"
        : "btn-primary text-xs"
  } inline-flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed`;

  if (titleEl) titleEl.textContent = title;
  if (subtitleEl) subtitleEl.textContent = subtitle;
  if (cancelBtn) cancelBtn.textContent = cancelLabel;
  if (submitTextEl) submitTextEl.textContent = confirmLabel;
  if (countEl) {
    countEl.textContent = finalEntries.length
      ? `${finalEntries.length} thay đổi sẽ được áp dụng`
      : inputField
        ? "Nhập thông tin và xác nhận"
        : "Xác nhận thực thi thao tác";
  }

  if (warningBox && warningList) {
    if (mergedWarnings.length) {
      warningBox.classList.remove("hidden");
      warningList.innerHTML = mergedWarnings.map((msg) => `<li>${escapeHtml(msg)}</li>`).join("");
    } else {
      warningBox.classList.add("hidden");
      warningList.innerHTML = "";
    }
  }

  // Ô nhập tùy chọn (thay thế hoàn toàn window.prompt)
  if (inputWrap && inputEl && inputLabel && inputErrEl) {
    if (inputField) {
      inputWrap.classList.remove("hidden");
      inputLabel.textContent = inputField.label || "Nhập giá trị mới";
      inputEl.type = inputField.type || "text";
      inputEl.placeholder = inputField.placeholder || "";
      inputEl.value = inputField.initialValue || "";
      inputErrEl.classList.add("hidden");
      inputErrEl.textContent = "";
    } else {
      inputWrap.classList.add("hidden");
      inputEl.value = "";
      inputErrEl.classList.add("hidden");
      inputErrEl.textContent = "";
    }
  }

  // Bảng Before / After (có hỗ trợ xem trước cuộn cho Prompt dài)
  if (tableWrap && tbody) {
    if (finalEntries.length) {
      tableWrap.classList.remove("hidden");
      tbody.innerHTML = finalEntries
        .map((row) => (row.isLongText ? renderLongTextDiffRow(row) : renderStandardDiffRow(row)))
        .join("");
    } else {
      tableWrap.classList.add("hidden");
      tbody.innerHTML = "";
    }
  }

  // Chạy lại hiệu ứng scale/fade-in mượt trên mỗi lần mở hộp thoại
  if (panel) {
    panel.classList.remove("ox-modal-enter");
    void panel.offsetWidth;
    panel.classList.add("ox-modal-enter");
  }
  modal.classList.remove("hidden");
  modal.classList.add("flex");
  if (inputField && inputEl) {
    setTimeout(() => inputEl.focus(), 40);
  }

  return new Promise((resolve) => {
    let isSaving = false;

    function setLoading(loading) {
      isSaving = loading;
      submitBtn.disabled = loading;
      cancelBtn.disabled = loading;
      if (closeBtn) closeBtn.disabled = loading;
      if (inputEl && inputField) inputEl.disabled = loading;
      if (spinnerEl) spinnerEl.classList.toggle("hidden", !loading);
      if (submitTextEl) submitTextEl.textContent = loading ? "Đang xử lý..." : confirmLabel;
    }

    function cleanupAndClose(outcome) {
      document.removeEventListener("keydown", onKeyDown);
      modal.removeEventListener("click", onBackdropClick);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeBtn) closeBtn.removeEventListener("click", onCancel);
      submitBtn.removeEventListener("click", onSubmit);
      if (inputEl) inputEl.removeEventListener("keydown", onInputEnter);
      setLoading(false);
      modal.classList.add("hidden");
      modal.classList.remove("flex");
      resolve(outcome);
    }

    function onCancel() {
      if (isSaving) return;
      cleanupAndClose({ confirmed: false, cancelled: true, changes: changesMap });
    }

    function onKeyDown(event) {
      if (event.key === "Escape" && !isSaving) {
        event.preventDefault();
        onCancel();
      }
    }

    function onBackdropClick(event) {
      if (event.target === modal && !isSaving) {
        onCancel();
      }
    }

    function onInputEnter(event) {
      if (event.key === "Enter" && !isSaving) {
        event.preventDefault();
        onSubmit();
      }
    }

    async function onSubmit() {
      if (isSaving) return;

      let inputValue = "";
      if (inputField && inputEl) {
        inputValue = inputField.trim === false ? inputEl.value : inputEl.value.trim();
        const minLen = Number(inputField.minLength || 0);
        if (minLen > 0 && inputValue.length < minLen) {
          if (inputErrEl) {
            inputErrEl.textContent =
              inputField.minLengthMessage || `Vui lòng nhập tối thiểu ${minLen} ký tự.`;
            inputErrEl.classList.remove("hidden");
          }
          inputEl.focus();
          return;
        }
        if (inputErrEl) inputErrEl.classList.add("hidden");
        if (inputField.fieldKey) {
          const isSecret = isSensitiveField(inputField.fieldKey);
          changesMap[inputField.fieldKey] = {
            old: inputField.beforeValue ?? (isSecret ? "••••• (đã lưu)" : "(trống)"),
            new: isSecret ? maskSecretPreview(inputValue) : inputValue,
          };
        }
      }

      if (!onConfirm) {
        cleanupAndClose({ confirmed: true, inputValue, changes: changesMap });
        return;
      }

      setLoading(true);
      try {
        const result = await onConfirm(changesMap, finalEntries, inputValue);
        cleanupAndClose({ confirmed: true, result, inputValue, changes: changesMap });
      } catch (err) {
        setLoading(false);
        showToast(err?.message || "Lưu thay đổi thất bại.", false);
      }
    }

    document.addEventListener("keydown", onKeyDown);
    modal.addEventListener("click", onBackdropClick);
    cancelBtn.addEventListener("click", onCancel);
    if (closeBtn) closeBtn.addEventListener("click", onCancel);
    submitBtn.addEventListener("click", onSubmit);
    if (inputField && inputEl) inputEl.addEventListener("keydown", onInputEnter);
  });
}

/**
 * Bộ chặn xác nhận lưu cấu hình tự động tính diff giữa `before` và `after`.
 * - Nếu 0 trường thay đổi -> báo Toast "Không có thay đổi nào để lưu" và không gửi request rỗng.
 * - Ngược lại mở `showConfirmModal` với bảng đối chiếu đầy đủ.
 */
export function confirmSaveChanges({
  title = "Xác nhận lưu thay đổi cấu hình",
  subtitle = "Kiểm tra kỹ các trường thay đổi trước khi áp dụng tức thì và ghi Audit Log",
  before,
  after,
  labels = {},
  units = {},
  customWarnings = [],
  confirmLabel = "Xác nhận lưu",
  confirmTone = "primary",
  onConfirm,
}) {
  const { entries, changesMap, warnings } = computeFormDiff(before, after, {
    labels,
    units,
    customWarnings,
  });

  if (entries.length === 0) {
    showToast("Không có thay đổi nào để lưu");
    return Promise.resolve({ confirmed: false, empty: true, changes: {} });
  }

  return showConfirmModal({
    title,
    subtitle,
    warnings,
    entries,
    changes: changesMap,
    confirmLabel,
    confirmTone,
    onConfirm,
  });
}
