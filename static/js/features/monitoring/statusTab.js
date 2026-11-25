/**
 * Bộ điều khiển Tab 1: Giám sát trạng thái Bot theo bố cục Oxfin Dark Admin
 * (Biểu đồ cột thống kê, 6 thẻ KPI hình thoi, biểu đồ đường cong độ trễ màu vàng và tiến độ RAM/MongoDB).
 */

import { apiRequest, escapeHtml, showToast } from "../../core/api.js";

function buildSmoothSplinePath(points) {
  if (!points.length) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;
  let d = `M ${points[0].x.toFixed(1)} ${points[0].y.toFixed(1)}`;
  for (let i = 0; i < points.length - 1; i += 1) {
    const p0 = points[i];
    const p1 = points[i + 1];
    const midX = (p0.x + p1.x) / 2;
    d += ` C ${midX.toFixed(1)} ${p0.y.toFixed(1)}, ${midX.toFixed(1)} ${p1.y.toFixed(1)}, ${p1.x.toFixed(1)} ${p1.y.toFixed(1)}`;
  }
  return d;
}

export function createStatusTabController() {
  let statusTimer = null;
  const latencyHistory = [];

  function renderUsageBarChart(stats) {
    const barsEl = document.getElementById("ox-usage-bars");
    const labelsEl = document.getElementById("ox-usage-labels");
    if (!barsEl || !labelsEl) return;

    // Ưu tiên hiển thị các tính năng & chỉ số chính theo 6 cột giống biểu đồ Oxfin
    const entries = [
      { label: "Tổng", value: stats.total || 0, primary: true },
      { label: "Thành công", value: stats.ok || 0, primary: false },
      { label: "Hôm nay", value: stats.today_total || 0, primary: false },
      { label: "Chat", value: stats.features?.chat || 0, primary: false },
      { label: "Web", value: stats.features?.web || 0, primary: false },
      { label: "Think/OCR", value: (stats.features?.think || 0) + (stats.features?.ocr || 0), primary: false },
    ];

    const maxVal = Math.max(10, ...entries.map((item) => item.value));
    const roundedMax = Math.ceil(maxVal / 10) * 10;
    const yMaxEl = document.getElementById("chart-y-max");
    const yMidEl = document.getElementById("chart-y-mid");
    if (yMaxEl) yMaxEl.textContent = String(roundedMax);
    if (yMidEl) yMidEl.textContent = String(Math.round(roundedMax / 2));

    barsEl.innerHTML = entries
      .map((item) => {
        const heightPct = item.value > 0 ? Math.max(16, Math.round((item.value / roundedMax) * 100)) : 14;
        return `
          <div class="ox-bar-col">
            <div class="ox-bar-rect ${item.primary ? "is-primary" : ""}" style="height:${heightPct}%" title="${escapeHtml(item.label)}: ${item.value}">
              <span>${item.value}</span>
            </div>
          </div>
        `;
      })
      .join("");

    labelsEl.innerHTML = entries
      .map((item) => `<span class="flex-1 text-center truncate">${escapeHtml(item.label)}</span>`)
      .join("");
  }

  function renderLatencySpline(pingMs, dbMs) {
    const pathEl = document.getElementById("ox-latency-path");
    const areaEl = document.getElementById("ox-latency-area");
    if (!pathEl || !areaEl) return;

    const sample = Number(pingMs ?? dbMs ?? 0);
    latencyHistory.push(Math.max(0, sample));
    if (latencyHistory.length > 10) {
      latencyHistory.shift();
    }

    // Nếu mới chỉ có 1 điểm dữ liệu, bổ sung nền mượt để đường cong hiển thị đẹp ngay từ lần tải đầu
    const series = latencyHistory.length >= 2 ? latencyHistory : [sample * 0.85, sample, sample * 0.95, sample];
    const maxVal = Math.max(50, ...series) * 1.2;
    const yMaxEl = document.getElementById("lat-y-max");
    const yMidEl = document.getElementById("lat-y-mid");
    if (yMaxEl) yMaxEl.textContent = String(Math.round(maxVal));
    if (yMidEl) yMidEl.textContent = String(Math.round(maxVal / 2));

    const leftX = 32;
    const rightX = 350;
    const topY = 16;
    const bottomY = 88;
    const stepX = series.length > 1 ? (rightX - leftX) / (series.length - 1) : 0;

    const points = series.map((val, idx) => ({
      x: leftX + idx * stepX,
      y: bottomY - Math.min(1, val / maxVal) * (bottomY - topY),
    }));

    const linePath = buildSmoothSplinePath(points);
    const areaPath = `${linePath} L ${points[points.length - 1].x.toFixed(1)} ${bottomY} L ${points[0].x.toFixed(1)} ${bottomY} Z`;
    pathEl.setAttribute("d", linePath);
    areaEl.setAttribute("d", areaPath);
  }

  async function loadStatus(checkGemini = false) {
    try {
      const data = await apiRequest(`/api/status${checkGemini ? "?check_gemini=1" : ""}`);
      const isOnline = data.bot.online;

      const headerBadge = document.getElementById("hdr-status-badge");
      headerBadge.textContent = isOnline ? "● Online" : "● Offline";
      headerBadge.className = `text-[11px] font-medium px-2.5 py-1 rounded-full border ${
        isOnline
          ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
          : "bg-rose-500/15 text-rose-300 border-rose-500/30"
      }`;

      document.getElementById("st-online").innerHTML = isOnline
        ? '<span class="text-emerald-400">Online</span>'
        : '<span class="text-rose-400">Offline</span>';
      document.getElementById("st-ping").textContent = data.bot.ping_ms !== null ? `${data.bot.ping_ms} ms` : "—";
      document.getElementById("st-botname").textContent = data.bot.username || "—";
      document.getElementById("st-uptime").textContent = data.bot.uptime_human || "0s";
      document.getElementById("st-guilds").textContent = String(data.bot.guild_count ?? 0);
      document.getElementById("st-version").textContent = `v${data.bot.version} · py ${data.bot.python_version}`;

      const modelsCountEl = document.getElementById("st-models-count");
      if (modelsCountEl) modelsCountEl.textContent = String(data.gemini.discovered_count ?? 0);
      const todayKpiEl = document.getElementById("st-today-kpi");
      if (todayKpiEl) todayKpiEl.textContent = String(data.stats.today_total ?? 0);

      // Tài nguyên RAM và CPU của container
      const res = data.resources;
      document.getElementById("st-ram").textContent = `${res.ram_used_mb} / ${res.ram_limit_mb} MB`;
      document.getElementById("st-ram-pct").textContent = `${res.ram_percent}%`;
      const ramBar = document.getElementById("st-ram-bar");
      ramBar.style.width = `${Math.min(100, res.ram_percent)}%`;
      ramBar.className = `ox-progress-fill ${
        res.ram_percent > 85 ? "bg-rose-500" : res.ram_percent > 65 ? "bg-amber-400" : "bg-emerald-500"
      }`;
      document.getElementById("st-ram-rss").textContent = `Tiến trình: ${res.ram_rss_mb} MB`;
      document.getElementById("st-cpu").textContent = `${res.cpu_percent}% CPU`;
      document.getElementById("st-threads").textContent = `${res.threads} luồng`;
      document.getElementById("st-requests-brief").textContent =
        `Hôm nay: ${data.stats.today_total} req · Tổng: ${data.stats.total} req`;

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
          const arrow = idx < data.gemini.auto_chain.length - 1 ? '<span class="text-[#5e6778]">→</span>' : "";
          return `<span class="px-2 py-1 rounded-md bg-[#0a0d12] border border-[#1e232e] font-mono text-[11px]">${escapeHtml(item.id)}${cd}</span>${arrow}`;
        })
        .join("");
      document.getElementById("st-gemini-meta").textContent =
        `${data.gemini.discovered_count} model chat` +
        (data.gemini.last_error ? ` · ⚠️ ${data.gemini.last_error}` : "");

      // Trạng thái MongoDB Atlas & bộ đệm đồng bộ RAM
      const db = data.mongodb;
      const dbBadge = document.getElementById("st-db-badge");
      dbBadge.textContent = !db.enabled ? "Chưa bật" : db.ok ? "🟢 Hoạt động" : "🔴 Lỗi";
      dbBadge.className = `text-[11px] px-2 py-0.5 rounded-full ${
        !db.enabled
          ? "bg-[#181d27] text-[#8b95a7]"
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

      const pendingCount = db.sync?.pending_count ?? 0;
      const syncDetailEl = document.getElementById("st-sync-detail");
      if (syncDetailEl) {
        syncDetailEl.textContent =
          pendingCount > 0
            ? `⚠️ Bộ đệm RAM: ${pendingCount} mục chờ đồng bộ Atlas`
            : "✓ Bộ đệm RAM đã đồng bộ hoàn toàn với Atlas";
      }
      const sidebarSyncEl = document.getElementById("sidebar-sync-badge");
      if (sidebarSyncEl) {
        sidebarSyncEl.textContent = pendingCount > 0 ? `${pendingCount} chờ sync` : "Đã đồng bộ";
        sidebarSyncEl.className = `font-mono ${pendingCount > 0 ? "text-amber-400" : "text-emerald-400"}`;
      }
      const pendingDot = document.getElementById("topbar-pending-dot");
      if (pendingDot) {
        pendingDot.textContent = String(pendingCount);
        pendingDot.classList.toggle("hidden", pendingCount <= 0);
      }

      // Cập nhật 2 biểu đồ đặc trưng Oxfin
      renderUsageBarChart(data.stats);
      renderLatencySpline(data.bot.ping_ms, db.ok ? db.latency_ms : null);

      // Chi tiết thống kê sử dụng
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
                `<div class="py-1.5 flex justify-between"><span class="text-slate-200">${escapeHtml(guild.name)}</span><span class="text-[#8b95a7] font-mono">${guild.member_count} tv</span></div>`
            )
            .join("")
        : '<div class="py-2 text-[#5e6778]">Chưa tham gia server nào hoặc bot đang khởi động.</div>';
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
