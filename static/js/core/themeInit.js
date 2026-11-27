/* Bộ nạp giao diện đồng bộ chống nhấp nháy (Zero-FOUC) chạy trước lần vẽ đầu tiên */
(function () {
  try {
    var saved = localStorage.getItem("kiwi_theme");
    var theme = saved === "dark" || saved === "light" || saved === "normal" ? saved : "normal";
    document.documentElement.dataset.theme = theme;
  } catch (_err) {
    document.documentElement.dataset.theme = "normal";
  }
})();
