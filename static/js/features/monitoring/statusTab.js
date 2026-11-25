/**
 * Bộ điều khiển Tab 1: Giám sát trạng thái Bot, tài nguyên, Gemini API, MongoDB và thống kê.
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

export function createStatusTabController() {
  let statusTimer = null;

  async function loadStatus(checkGemini = false) {
    try {
      const data = await apiRequest(`/api/status${checkGemini ? "?check_gemini=1" : ""}`);
      const isOnline = data.bot.online;

      const headerBadge = document.getElementById("hdr-status-badge");
      headerBadge.textContent = isOnline ? "● Online" : "● Offline";
      headerBadge.className = `text-[11px] px-2 py-0.5 rounded-full ${
        isOnline
          ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30"
          : "bg-rose-500/15 text-rose-300 border border-rose-500/30"
      }`;

      document.getElementById("st-online").innerHTML = isOnline
        ? '<span class="text-emerald-400">Online</span>'
        : '<span class="text-rose-400">Offline</span>';
      document.getElementById("st-ping").textContent = data.bot.ping_ms !== null ? `${data.bot.ping_ms} ms` : "—";
      document.getElementById("st-botname").textContent = data.bot.username || "—";
      document.getElementById("st-uptime").textContent = data.bot.uptime_human || "0s";
      document.getElementById("st-guilds").textContent = `${data.bot.guild_count} server`;
      document.getElementById("st-version").textContent = `v${data.bot.version} · py ${data.bot.python_version}`;

      // Tài nguyên RAM và CPU của container
      const res = data.resources;
      document.getElementById("st-ram").textContent = `${res.ram_used_mb} / ${res.ram_limit_mb} MB`;
      document.getElementById("st-ram-pct").textContent = `${res.ram_percent}%`;
      const ramBar = document.getElementById("st-ram-bar");
      ramBar.style.width = `${Math.min(100, res.ram_percent)}%`;
      ramBar.className = `h-full transition-all ${
        res.ram_percent > 85 ? "bg-rose-500" : res.ram_percent > 65 ? "bg-amber-500" : "bg-indigo-500"
      }`;
      document.getElementById("st-ram-rss").textContent = `Tiến trình bot: ${res.ram_rss_mb} MB`;

      document.getElementById("st-cpu").textContent = `${res.cpu_percent}%`;
      document.getElementById("st-threads").textContent = `${res.threads} luồng`;
      document.getElementById("st-requests-brief").textContent =
        `Hôm nay: ${data.stats.today_total} req · Tổng: ${data.stats.total}`;

      // Trạng thái Gemini API và chuỗi Auto
      if (data.gemini.check) {
        const check = data.gemini.check;
        document.getElementById("st-gemini-check").innerHTML = check.ok
          ? `<span class="text-emerald-400">🟢 Kết nối OK</span> · ${check.latency_ms} ms`
          : `<span class="text-rose-400">🔴 Lỗi (${escapeHtml(check.message)})</span> · ${check.latency_ms} ms`;
      }
      const chainEl = document.getElementById("st-auto-chain");
      chainEl.innerHTML = data.gemini.auto_chain
        .map((item, idx) => {
          const cd = item.cooldown_left > 0
            ? ` <span class="text-amber-300">⏳${Math.ceil(item.cooldown_left)}s</span>`
            : "";
          const arrow = idx < data.gemini.auto_chain.length - 1 ? '<span class="text-slate-600">→</span>' : "";
          return `<span class="px-2 py-1 rounded bg-slate-950 border border-slate-800 font-mono">${escapeHtml(item.id)}${cd}</span>${arrow}`;
        })
        .join("");
      document.getElementById("st-gemini-meta").textContent =
        `Đã phát hiện ${data.gemini.discovered_count} model chat` +
        (data.gemini.last_error ? ` · ⚠️ ${data.gemini.last_error}` : "");

      // Trạng thái MongoDB Atlas
      const db = data.mongodb;
      const dbBadge = document.getElementById("st-db-badge");
      dbBadge.textContent = !db.enabled ? "Chưa bật" : db.ok ? "🟢 Hoạt động" : "🔴 Lỗi";
      dbBadge.className = `text-xs px-2 py-0.5 rounded-full ${
        !db.enabled
          ? "bg-slate-800 text-slate-400"
          : db.ok
            ? "bg-emerald-500/15 text-emerald-300"
            : "bg-rose-500/15 text-rose-300"
      }`;
      document.getElementById("st-db-ms").textContent = db.ok ? `${db.latency_ms} ms` : "—";
      document.getElementById("st-db-active").textContent = db.active_sessions ?? "—";
      document.getElementById("st-db-archived").textContent = db.archived_sessions ?? "—";
      if (db.storage_mb !== null) {
        const pct = Math.min(100, (db.storage_mb / db.quota_mb) * 100);
        document.getElementById("st-db-storage").textContent = `${db.storage_mb} / ${db.quota_mb} MB (${pct.toFixed(1)}%)`;
        document.getElementById("st-db-bar").style.width = `${pct}%`;
      } else {
        document.getElementById("st-db-storage").textContent = "—";
      }

      // Thống kê sử dụng
      const stats = data.stats;
      document.getElementById("st-usage-summary").textContent =
        `Tổng request: ${stats.total} · Thành công: ${stats.success_rate !== null ? `${stats.success_rate}%` : "—"} · Lỗi: ${stats.errors} · Hôm nay (Pacific): ${stats.today_total}`;
      const dailyModelsText = Object.entries(stats.daily_models || {})
        .map(([modelId, count]) => `${modelId} (×${count})`)
        .join(", ") || "Chưa có";
      document.getElementById("st-usage-models").textContent = `Model dùng hôm nay: ${dailyModelsText}`;
      const featuresText = Object.entries(stats.features || {})
        .map(([feature, count]) => `${feature}: ${count}`)
        .join(" · ") || "—";
      document.getElementById("st-usage-features").textContent = `Tính năng: ${featuresText}`;
      document.getElementById("st-usage-err").textContent = stats.last_error
        ? `⚠️ Lỗi gần nhất: ${stats.last_error.message}`
        : "";

      // Danh sách Server Discord
      const guildListEl = document.getElementById("st-guild-list");
      guildListEl.innerHTML = data.bot.guilds.length
        ? data.bot.guilds
            .map(
              (guild) =>
                `<div class="py-1.5 flex justify-between"><span class="text-slate-200">${escapeHtml(guild.name)}</span><span class="text-slate-500 font-mono">${guild.member_count} tv</span></div>`
            )
            .join("")
        : '<div class="py-2 text-slate-500">Chưa tham gia server nào hoặc bot đang khởi động.</div>';
    } catch (err) {
      console.error(err);
    }
  }

  function bindEvents() {
    document.getElementById("btn-refresh-status").addEventListener("click", () => loadStatus(false));
    document.getElementById("btn-check-gemini").addEventListener("click", () => loadStatus(true));
    document.getElementById("btn-clear-cd").addEventListener("click", async () => {
      try {
        const res = await apiRequest("/api/models/clear-cooldowns", { method: "POST" });
        showToast(`Đã xóa cooldown của ${res.cleared} model.`);
        loadStatus(false);
      } catch (err) {
        showToast(err.message, false);
      }
    });
    document.getElementById("auto-refresh-toggle").addEventListener("change", (event) => {
      clearInterval(statusTimer);
      if (event.target.checked) {
        statusTimer = setInterval(() => loadStatus(false), 5000);
      }
    });
  }

  function startAutoRefresh() {
    clearInterval(statusTimer);
    statusTimer = setInterval(() => loadStatus(false), 5000);
  }

  return {
    bindEvents,
    loadStatus,
    startAutoRefresh,
  };
}
