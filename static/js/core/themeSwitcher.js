/**
 * Bộ điều khiển chuyển đổi 3 giao diện (Normal / Dark / Light) tức thì không cần tải lại trang.
 */

const VALID_THEMES = new Set(["normal", "dark", "light"]);
const STORAGE_KEY = "kiwi_theme";

export function getActiveTheme() {
  const fromDom = document.documentElement.dataset.theme;
  if (VALID_THEMES.has(fromDom)) return fromDom;
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (VALID_THEMES.has(saved)) return saved;
  } catch {
    // Bỏ qua lỗi quyền truy cập storage ở chế độ ẩn danh nghiêm ngặt
  }
  return "normal";
}

export function applyTheme(themeName) {
  const next = VALID_THEMES.has(themeName) ? themeName : "normal";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Bỏ qua nếu trình duyệt chặn ghi localStorage
  }
  document.querySelectorAll("[data-theme-option]").forEach((btn) => {
    const active = btn.dataset.themeOption === next;
    btn.setAttribute("aria-pressed", String(active));
    btn.dataset.active = String(active);
  });
  return next;
}

export function initThemeSwitcher() {
  const current = getActiveTheme();
  applyTheme(current);

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-theme-option]");
    if (!btn) return;
    applyTheme(btn.dataset.themeOption);
  });
}
