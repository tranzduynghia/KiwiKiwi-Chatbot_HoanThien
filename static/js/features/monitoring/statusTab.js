/**
 * Bộ điều khiển Tab 1: Giám sát trạng thái Bot theo bố cục Oxfin Dark Admin.
 * Hỗ trợ cập nhật nền im lặng mỗi 2000ms (không nhấp nháy, không đặt lại thanh cuộn, không chạy lại hiệu ứng tải đầu).
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

function setTextIfChanged(id, value) {
  const el = document.getElementById(id);
  if (!el) return;
  const next = String(value ?? "");
  if (el.textContent !== next) {
    el.textContent = next;
  }
}

export function createStatusTabController() {
  const latencyHistory = [];
  let isFetchingSystem = false;
  let isFetchingGemini = false;
  let barsInitialized = false;
  let lastChainHtml = "";
  let lastGuildsHtml = "";

  function renderUsageBarChart(stats) {
    const barsEl = document.getElementById("ox-usage-bars");
    const labelsEl = document.getElementById("ox-usage-labels");
    if (!barsEl || !labelsEl) return;

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
    setTextIfChanged("chart-y-max", roundedMax);
    setTextIfChanged("chart-y-mid", Math.round(roundedMax / 2));

    // Lần đầu tiên dựng cấu trúc DOM cột; các lần cập nhật nền 2s chỉ cập nhật chiều cao & số tại chỗ để chuyển động mượt
    if (!barsInitialized || barsEl.children.length !== entries.length) {
      barsEl.innerHTML = entries
        .map((item, idx) => {
          const heightPct = item.value > 0 ? Math.max(16, Math.round((item.value / roundedMax) * 100)) : 14;
          return `
            <div class="ox-bar-col">
              <div data-bar-idx="${idx}" class="ox-bar-rect ${item.primary ? "is-primary" : ""}" style="height:${heightPct}%" title="${escapeHtml(item.label)}: ${item.value}">
                <span data-bar-val="${idx}">${item.value}</span>
              </div>
            </div>
          `;
        })
        .join("");

      labelsEl.innerHTML = entries
        .map((item) => `<span class="flex-1 text-center truncate">${escapeHtml(item.label)}</span>`)
        .join("");
      barsInitialized = true;
      return;
    }

    entries.forEach((item, idx) => {
      const heightPct = item.value > 0 ? Math.max(16, Math.round((item.value / roundedMax) * 100)) : 14;
      const rectEl = barsEl.querySelector(`[data-bar-idx="${idx}"]`);
      const valEl = barsEl.querySelector(`[data-bar-val="${idx}"]`);
      if (rectEl) {
        rectEl.style.height = `${heightPct}%`;
        rectEl.title = `${item.label}: ${item.value}`;
      }
      if (valEl && valEl.textContent !== String(item.value)) {
        valEl.textContent = String(item.value);
      }
    });
  }

  function renderLatencySpline(pingMs, dbMs) {
    const pathEl = document.getElementById("ox-latency-path");
    const areaEl = document.getElementById("ox-latency-area");
    if (!pathEl || !areaEl) return;

    const sample = Number(pingMs ?? dbMs ?? 0);
    latencyHistory.push(Math.max(0, sample));
    if (latencyHistory.length > 14) {
      latencyHistory.shift();
    }

    const series = latencyHistory.length >= 2 ? latencyHistory : [sample * 0.85, sample, sample * 0.95, sample];
    const maxVal = Math.max(50, ...series) * 1.2;
    setTextIfChanged("lat-y-max", Math.round(maxVal));
    setTextIfChanged("lat-y-mid", Math.round(maxVal / 2));

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

  function syncAvatarElement(imgId, fallbackId, url, altName) {
    const imgEl = document.getElementById(imgId);
    const fallbackEl = document.getElementById(fallbackId);
    if (!imgEl || !fallbackEl) return;

    const cleanUrl = String(url || "").trim();
    if (!cleanUrl) {
      imgEl.classList.add("hidden");
      fallbackEl.classList.remove("hidden");
      return;
    }
    if (altName) imgEl.alt = altName;
    if (imgEl.dataset.loadedSrc !== cleanUrl) {
      imgEl.dataset.loadedSrc = cleanUrl;
      imgEl.src = cleanUrl;
    }
    if (!imgEl.dataset.hasError) {
      imgEl.classList.remove("hidden");
      fallbackEl.classList.add("hidden");
    }
  }

  function bindAvatarFallbacks() {
    [
      ["brand-bot-avatar", "brand-bot-avatar-fallback"],
      ["presence-preview-avatar", "presence-preview-avatar-fallback"],
    ].forEach(([imgId, fallbackId]) => {
      const imgEl = document.getElementById(imgId);
      const fallbackEl = document.getElementById(fallbackId);
      if (!imgEl || !fallbackEl) return;
      imgEl.addEventListener("error", () => {
        imgEl.dataset.hasError = "1";
        imgEl.classList.add("hidden");
        fallbackEl.classList.remove("hidden");
      });
      imgEl.addEventListener("load", () => {
        delete imgEl.dataset.hasError;
        imgEl.classList.remove("hidden");
        fallbackEl.classList.add("hidden");
      });
    });
  }

  async function loadStatus(checkGemini = false, { silent = false } = {}) {
    if (checkGemini) {
      if (isFetchingGemini && silent) return;
      isFetchingGemini = true;
    } else {
      if (isFetchingSystem && silent) return;
      isFetchingSystem = true;
    }
    try {
      const data = await apiRequest(`/api/status${checkGemini ? "?check_gemini=1" : ""}`);
      const isOnline = data.bot.online;

      const headerBadge = document.getElementById("hdr-status-badge");
      if (headerBadge) {
        const badgeText = isOnline ? "● Online" : "● Offline";
        if (headerBadge.textContent !== badgeText) headerBadge.textContent = badgeText;
        headerBadge.className = `text-[11px] font-medium px-2.5 py-1 rounded-full border transition-colors duration-300 ${
          isOnline
            ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
            : "bg-rose-500/15 text-rose-300 border-rose-500/30"
        }`;
      }

      const stOnlineEl = document.getElementById("st-online");
      if (stOnlineEl) {
        const onlineHtml = isOnline
          ? '<span class="text-emerald-400">Online</span>'
          : '<span class="text-rose-400">Offline</span>';
        if (stOnlineEl.innerHTML !== onlineHtml) stOnlineEl.innerHTML = onlineHtml;
      }

      const botDisplayName = data.bot.display_name || data.bot.username || "KiwiKiwi";
      setTextIfChanged("st-ping", data.bot.ping_ms !== null ? `${data.bot.ping_ms} ms` : "—");
      setTextIfChanged("st-botname", data.bot.username || "—");
      setTextIfChanged("brand-bot-name", botDisplayName);
      setTextIfChanged("presence-preview-botname", botDisplayName);
      syncAvatarElement("brand-bot-avatar", "brand-bot-avatar-fallback", data.bot.avatar_url, botDisplayName);
      syncAvatarElement("presence-preview-avatar", "presence-preview-avatar-fallback", data.bot.avatar_url, botDisplayName);
      setTextIfChanged("st-uptime", data.bot.uptime_human || "0s");
      setTextIfChanged("st-guilds", String(data.bot.guild_count ?? 0));
      setTextIfChanged("st-version", `v${data.bot.version} · py ${data.bot.python_version}`);
      setTextIfChanged("st-models-count", String(data.gemini.discovered_count ?? 0));
      setTextIfChanged("st-today-kpi", String(data.stats.today_total ?? 0));

      // Tài nguyên RAM và CPU của container
      const res = data.resources;
      setTextIfChanged("st-ram", `${res.ram_used_mb} / ${res.ram_limit_mb} MB`);
      setTextIfChanged("st-ram-pct", `${res.ram_percent}%`);
      const ramBar = document.getElementById("st-ram-bar");
      if (ramBar) {
        ramBar.style.width = `${Math.min(100, res.ram_percent)}%`;
        ramBar.className = `ox-progress-fill ${
          res.ram_percent > 85 ? "bg-rose-500" : res.ram_percent > 65 ? "bg-amber-400" : "bg-emerald-500"
        }`;
      }
      setTextIfChanged("st-ram-rss", `Tiến trình: ${res.ram_rss_mb} MB`);
      setTextIfChanged("st-cpu", `${res.cpu_percent}% CPU`);
      setTextIfChanged("st-threads", `${res.threads} luồng`);
      setTextIfChanged("st-requests-brief", `Hôm nay: ${data.stats.today_total} req · Tổng: ${data.stats.total} req`);

      // Trạng thái Gemini API và chuỗi Auto
      if (data.gemini.check) {
        const check = data.gemini.check;
        const checkEl = document.getElementById("st-gemini-check");
        if (checkEl) {
          checkEl.innerHTML = check.ok
            ? `<span class="text-emerald-400">🟢 Kết nối OK</span> · ${check.latency_ms} ms`
            : `<span class="text-rose-400">🔴 Lỗi (${escapeHtml(check.message)})</span> · ${check.latency_ms} ms`;
        }
      }
      const chainHtml = data.gemini.auto_chain
        .map((item, idx) => {
          const cd = item.cooldown_left > 0
            ? ` <span class="text-amber-300">⏳${Math.ceil(item.cooldown_left)}s</span>`
            : "";
          const arrow = idx < data.gemini.auto_chain.length - 1 ? '<span class="text-[#5e6778]">→</span>' : "";
          return `<span class="px-2 py-1 rounded-md bg-[#0a0d12] border border-[#1e232e] font-mono text-[11px]">${escapeHtml(item.id)}${cd}</span>${arrow}`;
        })
        .join("");
      if (chainHtml !== lastChainHtml) {
        const chainEl = document.getElementById("st-auto-chain");
        if (chainEl) chainEl.innerHTML = chainHtml;
        lastChainHtml = chainHtml;
      }
      setTextIfChanged(
        "st-gemini-meta",
        `${data.gemini.discovered_count} model chat` + (data.gemini.last_error ? ` · ⚠️ ${data.gemini.last_error}` : "")
      );

      // Trạng thái MongoDB Atlas & bộ đệm đồng bộ RAM
      const db = data.mongodb;
      const dbBadge = document.getElementById("st-db-badge");
      if (dbBadge) {
        const dbBadgeText = !db.enabled ? "Chưa bật" : db.ok ? "🟢 Hoạt động" : "🔴 Lỗi";
        if (dbBadge.textContent !== dbBadgeText) dbBadge.textContent = dbBadgeText;
        dbBadge.className = `text-[11px] px-2 py-0.5 rounded-full ${
          !db.enabled
            ? "bg-[#181d27] text-[#8b95a7]"
            : db.ok
              ? "bg-emerald-500/15 text-emerald-300"
              : "bg-rose-500/15 text-rose-300"
        }`;
      }
      setTextIfChanged("st-db-ms", db.ok ? `${db.latency_ms} ms` : "—");
      setTextIfChanged("st-db-active", db.active_sessions ?? "—");
      setTextIfChanged("st-db-archived", db.archived_sessions ?? "—");
      if (db.storage_mb !== null) {
        const pct = Math.min(100, (db.storage_mb / db.quota_mb) * 100);
        setTextIfChanged("st-db-storage", `${db.storage_mb} / ${db.quota_mb} MB (${pct.toFixed(1)}%)`);
        const dbBar = document.getElementById("st-db-bar");
        if (dbBar) dbBar.style.width = `${pct}%`;
      } else {
        setTextIfChanged("st-db-storage", "—");
      }

      const pendingCount = db.sync?.pending_count ?? 0;
      setTextIfChanged(
        "st-sync-detail",
        pendingCount > 0
          ? `⚠️ Bộ đệm RAM: ${pendingCount} mục chờ đồng bộ Atlas`
          : "✓ Bộ đệm RAM đã đồng bộ hoàn toàn với Atlas"
      );
      const sidebarSyncEl = document.getElementById("sidebar-sync-badge");
      if (sidebarSyncEl) {
        const syncLabel = pendingCount > 0 ? `${pendingCount} chờ sync` : "Đã đồng bộ";
        if (sidebarSyncEl.textContent !== syncLabel) sidebarSyncEl.textContent = syncLabel;
        sidebarSyncEl.className = `font-mono ${pendingCount > 0 ? "text-amber-400" : "text-emerald-400"}`;
      }
      const pendingDot = document.getElementById("topbar-pending-dot");
      if (pendingDot) {
        pendingDot.textContent = String(pendingCount);
        pendingDot.classList.toggle("hidden", pendingCount <= 0);
      }

      // Cập nhật mượt 2 biểu đồ tại chỗ
      renderUsageBarChart(data.stats);
      renderLatencySpline(data.bot.ping_ms, db.ok ? db.latency_ms : null);

      // Chi tiết thống kê sử dụng
      const stats = data.stats;
      setTextIfChanged(
        "st-usage-summary",
        `Tổng request: ${stats.total} · Thành công: ${stats.success_rate !== null ? `${stats.success_rate}%` : "—"} · Lỗi: ${stats.errors} · Hôm nay (Pacific): ${stats.today_total}`
      );
      const dailyModelsText = Object.entries(stats.daily_models || {})
        .map(([modelId, count]) => `${modelId} (×${count})`)
        .join(", ") || "Chưa có";
      setTextIfChanged("st-usage-models", `Model dùng hôm nay: ${dailyModelsText}`);
      const featuresText = Object.entries(stats.features || {})
        .map(([feature, count]) => `${feature}: ${count}`)
        .join(" · ") || "—";
      setTextIfChanged("st-usage-features", `Tính năng: ${featuresText}`);
      setTextIfChanged("st-usage-err", stats.last_error ? `⚠️ Lỗi gần nhất: ${stats.last_error.message}` : "");

      // Danh sách Server Discord (chỉ cập nhật DOM khi nội dung thực sự thay đổi để giữ nguyên vị trí cuộn)
      const guildsHtml = data.bot.guilds.length
        ? data.bot.guilds
            .map(
              (guild) =>
                `<div class="py-1.5 flex justify-between"><span class="text-slate-200">${escapeHtml(guild.name)}</span><span class="text-[#8b95a7] font-mono">${guild.member_count} tv</span></div>`
            )
            .join("")
        : '<div class="py-2 text-[#5e6778]">Chưa tham gia server nào hoặc bot đang khởi động.</div>';
      if (guildsHtml !== lastGuildsHtml) {
        const guildListEl = document.getElementById("st-guild-list");
        if (guildListEl) guildListEl.innerHTML = guildsHtml;
        lastGuildsHtml = guildsHtml;
      }
    } catch (err) {
      if (!silent) console.error(err);
    } finally {
      if (checkGemini) isFetchingGemini = false;
      else isFetchingSystem = false;
    }
  }

  function bindEvents() {
    bindAvatarFallbacks();
    document.getElementById("btn-refresh-status").addEventListener("click", () => loadStatus(false, { silent: false }));
    document.getElementById("btn-check-gemini").addEventListener("click", () => loadStatus(true, { silent: false }));
    document.getElementById("btn-clear-cd").addEventListener("click", async () => {
      try {
        const res = await apiRequest("/api/models/clear-cooldowns", { method: "POST" });
        showToast(`Đã xóa cooldown của ${res.cleared} model.`);
        loadStatus(false, { silent: true });
      } catch (err) {
        showToast(err.message, false);
      }
    });
  }

  return {
    bindEvents,
    loadStatus,
  };
}
