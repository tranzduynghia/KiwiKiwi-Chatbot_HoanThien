/**
 * Tiện ích lõi cho tầng giao diện: gọi API JSON, hiển thị Toast và chống XSS khi render HTML.
 */

export function formatErrorMessage(detail, fallback = "Đã xảy ra lỗi không xác định.") {
  if (detail == null || detail === "") return fallback;
  if (typeof detail === "string") return detail;
  if (detail instanceof Error) return formatErrorMessage(detail.message, fallback);
  if (Array.isArray(detail)) {
    const first = detail[0];
    if (first && typeof first === "object" && typeof first.msg === "string" && first.msg.trim()) {
      const loc = Array.isArray(first.loc) ? first.loc.filter((part) => part !== "body" && part !== "query").join(".") : "";
      return loc ? `${loc}: ${first.msg}` : first.msg;
    }
    const parts = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") return item.msg || item.message || JSON.stringify(item);
        return String(item);
      })
      .filter(Boolean);
    return parts.length ? parts.join("; ") : JSON.stringify(detail);
  }
  if (typeof detail === "object") {
    if (typeof detail.msg === "string" && detail.msg.trim()) return detail.msg;
    if (typeof detail.message === "string" && detail.message.trim()) return detail.message;
    if ("detail" in detail) return formatErrorMessage(detail.detail, fallback);
    try {
      return JSON.stringify(detail);
    } catch {
      return fallback;
    }
  }
  return String(detail);
}

export function showToast(message, isSuccess = true) {
  const toastEl = document.getElementById("toast");
  if (!toastEl) return;
  toastEl.textContent = formatErrorMessage(message, isSuccess ? "Thành công." : "Đã xảy ra lỗi.");
  toastEl.className = `fixed bottom-5 right-5 z-50 max-w-sm px-4 py-3 rounded-xl text-sm border ${
    isSuccess ? "ox-toast-success" : "ox-toast-error"
  }`;
  toastEl.classList.remove("hidden");
  clearTimeout(toastEl._timerId);
  toastEl._timerId = setTimeout(() => toastEl.classList.add("hidden"), 3500);
}

let _onStepUpRequired = null;

export function setStepUpRequiredHandler(handler) {
  _onStepUpRequired = typeof handler === "function" ? handler : null;
}

export async function apiRequest(url, options = {}) {
  const { redirectOn401 = true, ...fetchOptions } = options;
  let response;
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      redirect: "manual",
      ...fetchOptions,
      headers: { "Content-Type": "application/json", ...(fetchOptions.headers || {}) },
    });
  } catch (networkErr) {
    const err = new Error("Không thể kết nối tới máy chủ (lỗi mạng hoặc bị chặn chuyển hướng). Vui lòng tải lại trang.");
    err.cause = networkErr;
    throw err;
  }
  if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
    if (redirectOn401) {
      window.location.reload();
    }
    const err = new Error("Phiên xác thực đã hết hạn hoặc bị chuyển hướng (HTTP 302). Vui lòng tải lại trang.");
    err.status = response.status || 302;
    throw err;
  }
  if (response.status === 401 && redirectOn401) {
    window.location.href = "/login";
    throw new Error("Phiên đăng nhập đã hết hạn.");
  }
  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const data = isJson ? await response.json().catch(() => ({})) : {};
const ERROR_CODE_MESSAGES = {
  UNAUTHORIZED: "Phiên đăng nhập không hợp lệ hoặc đã hết hạn.",
  LOCKED: "Mục này đang được khóa. Vui lòng xác thực lại để tiếp tục.",
  INVALID_INPUT: "Dữ liệu gửi lên không hợp lệ. Vui lòng kiểm tra lại.",
  NOT_FOUND: "Không tìm thấy tài nguyên được yêu cầu.",
  PAYLOAD_TOO_LARGE: "Kích thước dữ liệu vượt quá giới hạn cho phép.",
  RATE_LIMITED: "Thao tác quá nhanh. Vui lòng thử lại sau ít giây.",
  SERVER_ERROR: "Đã xảy ra lỗi nội bộ máy chủ. Vui lòng thử lại.",
};

  if (response.status === 403 && data && (data.detail === "STEP_UP_REQUIRED" || data.code === "LOCKED" || data.locked === true)) {
    if (_onStepUpRequired) {
      _onStepUpRequired();
    }
    const err = new Error("Vui lòng xác thực lại (Step-up Auth) để tiếp tục.");
    err.code = "STEP_UP_REQUIRED";
    err.status = 403;
    throw err;
  }
  if (!response.ok) {
    const codeMsg = data && data.code ? ERROR_CODE_MESSAGES[data.code] : "";
    const fallback = isJson
      ? (codeMsg || `Lỗi HTTP ${response.status}`)
      : `Máy chủ trả về phản hồi không phải JSON (HTTP ${response.status}).`;
    const errMessage = formatErrorMessage(data.detail ?? data.message ?? codeMsg, fallback);
    const err = new Error(errMessage);
    err.status = response.status;
    err.code = data && data.code ? data.code : undefined;
    throw err;
  }
  return data;
}

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

export function sanitizeHttpUrl(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  try {
    const parsed = new URL(raw);
    return parsed.protocol === "https:" ? parsed.href : "";
  } catch {
    return "";
  }
}
