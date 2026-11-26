/**
 * Bộ chặn xác nhận lưu cấu hình toàn cục (`confirmSaveChanges`):
 * - Tính toán chính xác diff từng trường giữa trạng thái tải ban đầu (`before`) và trạng thái biểu mẫu (`after`)
 * - Bỏ qua trường không đổi; nếu 0 thay đổi thì báo Toast "Không có thay đổi nào để lưu"
 * - Che giá trị nhạy cảm dạng tiền tố (`eyJh•••••`), hiển thị cảnh báo vàng với cài đặt tác động cao
 * - Trả về đối tượng `{ field: { old, new } }` kèm payload lưu để Backend Audit Log ghi lại đầy đủ
 */

import { escapeHtml, showToast } from "./api.js";

const SENSITIVE_KEY_TOKENS = ["password", "secret", "token", "api_key", "apikey", "uri"];

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
  "models.default_text_model": "Model mặc định",
  "models.auto_model_chain": "Chuỗi Auto Model",
  "models.auto_last_model": "Model dự phòng cuối chuỗi Auto",
  "models.hidden_models": "Mẫu wildcard model bị ẩn",
  "models.hide_pro_models": "Ẩn các model Pro",
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
  const lowered = String(key || "").toLowerCase();
  return SENSITIVE_KEY_TOKENS.some((tok) => lowered.includes(tok));
}

export function maskSecretPreview(value) {
  if (value === null || value === undefined || value === "") return "(trống)";
  const text = String(value).trim();
  if (!text) return "(trống)";
  if (text.includes("••••") || text.startsWith("(")) return text;
  if (text.length <= 4) return "•••••";
  return `${text.slice(0, 4)}•••••`;
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

function formatDisplayValue(key, value, { units = {}, isSecret = false } = {}) {
  if (isSecret) return maskSecretPreview(value);
  if (value === null || value === undefined || value === "") return "(trống)";
  if (typeof value === "boolean") return value ? "Bật" : "Tắt";
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "(trống)";
  }
  if (isPlainObject(value)) {
    const raw = JSON.stringify(value);
    return raw.length > 160 ? `${raw.slice(0, 160)}…` : raw;
  }
  if (typeof value === "number") {
    const unit = units[key] ?? DEFAULT_FIELD_UNITS[key] ?? "";
    return `${value}${unit}`;
  }
  const text = String(value);
  return text.length > 180 ? `${text.slice(0, 180)}… (${text.length} ký tự)` : text;
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
    const label = labels[item.key] || DEFAULT_FIELD_LABELS[item.key] || item.key;
    const warning = detectHighImpactWarning(item.key, item.before, item.after);
    if (warning && !warnings.includes(warning)) {
      warnings.push(warning);
    }

    const safeOld = secret ? maskSecretPreview(item.before) : item.before ?? null;
    const safeNew = secret ? maskSecretPreview(item.after) : item.after ?? null;
    changesMap[item.key] = { old: safeOld, new: safeNew };

    entries.push({
      key: item.key,
      label,
      before: item.before,
      after: item.after,
      formattedBefore: formatDisplayValue(item.key, item.before, { units, isSecret: secret }),
      formattedAfter: formatDisplayValue(item.key, item.after, { units, isSecret: secret }),
      isSensitive: secret,
      isHighImpact: Boolean(warning),
      warning,
    });
  }

  return { entries, changesMap, warnings };
}

/**
 * Hiển thị hộp thoại xác nhận lưu toàn cục (#confirm-save-modal) với bảng Before/After.
 * - Tự động hủy và hiện Toast nếu không có trường nào thay đổi.
 * - Khi người dùng bấm "Xác nhận lưu", hiển thị spinner và gọi `onConfirm(changesMap, entries)`.
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

  const modal = document.getElementById("confirm-save-modal");
  const titleEl = document.getElementById("confirm-save-title");
  const subtitleEl = document.getElementById("confirm-save-subtitle");
  const warningBox = document.getElementById("confirm-save-warning");
  const warningList = document.getElementById("confirm-save-warning-list");
  const tbody = document.getElementById("confirm-save-tbody");
  const countEl = document.getElementById("confirm-save-count");
  const cancelBtn = document.getElementById("btn-cancel-confirm-save");
  const closeBtn = document.getElementById("btn-close-confirm-save");
  const submitBtn = document.getElementById("btn-submit-confirm-save");
  const spinnerEl = document.getElementById("confirm-save-spinner");
  const submitTextEl = document.getElementById("confirm-save-btn-text");

  if (!modal || !tbody || !submitBtn || !cancelBtn) {
    // Fallback an toàn nếu DOM modal chưa tồn tại
    return Promise.resolve(onConfirm ? onConfirm(changesMap, entries) : null).then((result) => ({
      confirmed: true,
      result,
      changes: changesMap,
    }));
  }

  if (titleEl) titleEl.textContent = title;
  if (subtitleEl) subtitleEl.textContent = subtitle;
  if (countEl) countEl.textContent = `${entries.length} thay đổi sẽ được áp dụng`;
  if (submitTextEl) submitTextEl.textContent = confirmLabel;

  if (warningBox && warningList) {
    if (warnings.length) {
      warningBox.classList.remove("hidden");
      warningList.innerHTML = warnings.map((msg) => `<li>${escapeHtml(msg)}</li>`).join("");
    } else {
      warningBox.classList.add("hidden");
      warningList.innerHTML = "";
    }
  }

  tbody.innerHTML = entries
    .map((row) => {
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
    })
    .join("");

  modal.classList.remove("hidden");
  modal.classList.add("flex");

  return new Promise((resolve) => {
    let isSaving = false;

    function setLoading(loading) {
      isSaving = loading;
      submitBtn.disabled = loading;
      cancelBtn.disabled = loading;
      if (closeBtn) closeBtn.disabled = loading;
      if (spinnerEl) spinnerEl.classList.toggle("hidden", !loading);
      if (submitTextEl) submitTextEl.textContent = loading ? "Đang lưu..." : confirmLabel;
    }

    function cleanupAndClose(outcome) {
      document.removeEventListener("keydown", onKeyDown);
      modal.removeEventListener("click", onBackdropClick);
      cancelBtn.removeEventListener("click", onCancel);
      if (closeBtn) closeBtn.removeEventListener("click", onCancel);
      submitBtn.removeEventListener("click", onSubmit);
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

    async function onSubmit() {
      if (isSaving) return;
      if (!onConfirm) {
        cleanupAndClose({ confirmed: true, changes: changesMap });
        return;
      }
      setLoading(true);
      try {
        const result = await onConfirm(changesMap, entries);
        cleanupAndClose({ confirmed: true, result, changes: changesMap });
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
  });
}
