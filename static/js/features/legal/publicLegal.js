/**
 * Khởi tạo nút chuyển 3 giao diện (Normal / Dark / Light), định dạng mốc giờ theo múi giờ người xem
 * cho trang Pháp lý công khai được đệm tại Cloudflare Edge và xử lý ảnh đại diện tác giả dự phòng.
 */

import { initThemeSwitcher } from "../../core/themeSwitcher.js";

initThemeSwitcher();

function formatLocalMinute(isoUtc) {
  const dt = new Date(isoUtc);
  if (Number.isNaN(dt.getTime())) return "";
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: tz,
      hour: "2-digit",
      minute: "2-digit",
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour12: false,
    }).formatToParts(dt);
    const map = Object.fromEntries(parts.map((p) => [p.type, p.value]));
    if (map.hour && map.minute && map.day && map.month && map.year) {
      return `${map.hour}:${map.minute} ${map.day}/${map.month}/${map.year}`;
    }
  } catch {
    // Giữ nguyên chuỗi đã render từ server theo CF-Timezone
  }
  return "";
}

document.querySelectorAll(".js-local-minute-ts[data-utc-ts]").forEach((el) => {
  const raw = (el.getAttribute("data-utc-ts") || "").trim();
  if (!raw) return;
  const formatted = formatLocalMinute(raw);
  if (formatted) {
    el.textContent = formatted;
  }
});

document.querySelectorAll(".js-author-avatar").forEach((img) => {
  img.addEventListener("error", () => {
    img.classList.add("hidden");
    const fallback = img.nextElementSibling;
    if (fallback && fallback.classList.contains("js-author-avatar-fallback")) {
      fallback.classList.remove("hidden");
      fallback.classList.add("inline-flex");
    }
  });
});
