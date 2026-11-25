/**
 * Tiện ích lõi cho tầng giao diện: gọi API JSON, hiển thị Toast và chống XSS khi render HTML.
 */

export function showToast(message, isSuccess = true) {
  const toastEl = document.getElementById("toast");
  if (!toastEl) return;
  toastEl.textContent = message;
  toastEl.className = `fixed bottom-5 right-5 z-50 max-w-sm px-4 py-3 rounded-xl text-sm shadow-xl border ${
    isSuccess
      ? "bg-emerald-950/95 border-emerald-500/40 text-emerald-200"
      : "bg-rose-950/95 border-rose-500/40 text-rose-200"
  }`;
  toastEl.classList.remove("hidden");
  clearTimeout(toastEl._timerId);
  toastEl._timerId = setTimeout(() => toastEl.classList.add("hidden"), 3500);
}

export async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Phiên đăng nhập đã hết hạn.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `Lỗi HTTP ${response.status}`);
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
