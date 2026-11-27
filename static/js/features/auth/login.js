/**
 * Xử lý biểu mẫu đăng nhập truyền thống bằng tên đăng nhập và mật khẩu.
 */

import { initThemeSwitcher } from "../../core/themeSwitcher.js";

initThemeSwitcher();

const loginForm = document.getElementById("login-form");
const errorBox = document.getElementById("client-error");
const submitBtn = document.getElementById("submit-btn");
const loginAvatar = document.getElementById("login-bot-avatar");
const loginAvatarFallback = document.getElementById("login-bot-avatar-fallback");

if (loginAvatar && loginAvatarFallback) {
  loginAvatar.addEventListener("error", () => {
    loginAvatar.classList.add("hidden");
    loginAvatarFallback.classList.remove("hidden");
  });
}

if (loginForm) {
  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.classList.add("hidden");
    submitBtn.disabled = true;
    submitBtn.textContent = "Đang xác thực...";

    try {
      const response = await fetch("/auth/login", {
        method: "POST",
        credentials: "same-origin",
        redirect: "manual",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: document.getElementById("username").value.trim(),
          password: document.getElementById("password").value,
        }),
      });
      if (response.type === "opaqueredirect" || (response.status >= 300 && response.status < 400)) {
        window.location.reload();
        throw new Error("Phiên Cloudflare Access đã hết hạn, đang tải lại trang...");
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail || `Đăng nhập thất bại (HTTP ${response.status}).`);
      }
      window.location.href = "/dashboard";
    } catch (err) {
      errorBox.textContent =
        err instanceof TypeError
          ? "Không thể kết nối tới máy chủ xác thực (lỗi mạng hoặc bị chặn chuyển hướng). Vui lòng tải lại trang."
          : err.message;
      errorBox.classList.remove("hidden");
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Đăng nhập";
    }
  });
}
