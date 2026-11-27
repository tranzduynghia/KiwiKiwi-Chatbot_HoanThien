/**
 * Bộ điều khiển thẻ "Cloudflare Tunnel (cloudflared)":
 * - Chọn chế độ Named Tunnel (Token) hoặc Quick Tunnel (trycloudflare.com)
 * - Nhập Token ẩn mật khẩu, bật/tắt khóa "Chỉ nhận truy cập qua Cloudflare Tunnel" và "Tin cậy Proxy Headers"
 * - Nút trên cùng: nút nguồn động Bật Tunnel (xanh lục) <-> Tắt Tunnel (đỏ) tự đồng bộ ngay khi bật/tắt/poll
 * - Nút phía dưới: Cập nhật Tunnel để lưu thay đổi Mode/Token và khởi động lại nóng cloudflared
 * - Mọi thao tác (Bật, Tắt, Cập nhật) đều bắt buộc đi qua Hộp thoại Xác nhận trong ứng dụng (`showConfirmModal`)
 */

import { apiRequest, sanitizeHttpUrl, showToast } from "../../core/api.js";
import { computeFormDiff, confirmSaveChanges, showConfirmModal } from "../../core/confirmSave.js";

const TOGGLE_BTN_BASE_CLASS =
  "w-full inline-flex items-center justify-center gap-2 px-3.5 py-2 rounded-lg text-xs font-semibold text-white border shadow-sm transition-colors duration-200 disabled:opacity-50 disabled:cursor-not-allowed";

const TOGGLE_BTN_START_CLASS = `${TOGGLE_BTN_BASE_CLASS} bg-emerald-600 hover:bg-emerald-500 border-emerald-500/40`;
const TOGGLE_BTN_STOP_CLASS = `${TOGGLE_BTN_BASE_CLASS} bg-rose-600 hover:bg-rose-500 border-rose-500/40`;

function formatModeLabel(mode) {
  return mode === "quick" ? "Quick Tunnel (trycloudflare.com)" : "Named Tunnel (Token)";
}

function normalizeOriginUrl(raw) {
  return String(raw || "")
    .replace(/^(?:Origin\s*nội\s*bộ\s*:|Origin\s*:)\s*/i, "")
    .trim();
}

export function createTunnelCardController({ isAdmin, onConfigSaved }) {
  let hasSavedToken = false;
  let hasTelemetry = false;
  let isTunnelActive = false;
  let isTogglingWarp = false;
  let initialState = null;

  function syncModeInputs() {
    const modeEl = document.getElementById("cf-tunnel-mode");
    const tokenWrapEl = document.getElementById("cf-tunnel-token-wrap");
    const tokenEl = document.getElementById("cf-tunnel-token");
    if (!modeEl || !tokenWrapEl || !tokenEl) return;

    const isQuick = modeEl.value === "quick";
    tokenWrapEl.classList.toggle("opacity-50", isQuick);
    tokenEl.disabled = isQuick || !isAdmin;
  }

  function syncToggleButton(active) {
    isTunnelActive = Boolean(active);
    const toggleBtn = document.getElementById("btn-toggle-tunnel");
    const labelEl = document.getElementById("btn-toggle-tunnel-label");
    if (!toggleBtn) return;

    toggleBtn.dataset.tunnelActive = String(isTunnelActive);
    toggleBtn.className = isTunnelActive ? TOGGLE_BTN_STOP_CLASS : TOGGLE_BTN_START_CLASS;
    if (labelEl) {
      const nextLabel = isTunnelActive ? "Tắt Tunnel" : "Bật Tunnel";
      if (labelEl.textContent !== nextLabel) labelEl.textContent = nextLabel;
    }
  }

  function fill(config) {
    const tunnel = config?.tunnel || {};
    hasSavedToken = Boolean(tunnel.has_cf_tunnel_token ?? tunnel.cf_tunnel_has_token);

    const modeEl = document.getElementById("cf-tunnel-mode");
    const tokenEl = document.getElementById("cf-tunnel-token");
    const tokenHintEl = document.getElementById("cf-token-hint");
    const inboundLockEl = document.getElementById("cf-tunnel-only-inbound");
    const proxyHeadersEl = document.getElementById("cf-proxy-headers");
    const warpToggleEl = document.getElementById("cf-warp-outbound-toggle");

    const loadedMode = tunnel.cf_tunnel_mode === "quick" ? "quick" : "named";
    const warpEnabled = tunnel.warp_outbound_enabled !== undefined ? Boolean(tunnel.warp_outbound_enabled) : true;
    if (modeEl) modeEl.value = loadedMode;
    if (tokenEl) {
      tokenEl.value = "";
      tokenEl.placeholder = hasSavedToken
        ? "•••••••••••• (Đã lưu Token — để trống nếu giữ nguyên)"
        : "Dán Cloudflare Tunnel Token";
    }
    if (tokenHintEl) {
      tokenHintEl.textContent = hasSavedToken
        ? "Đã lưu Token bảo mật trong MongoDB"
        : "Chưa lưu Token";
      tokenHintEl.className = `text-[10px] ${hasSavedToken ? "text-emerald-400" : "text-[#5e6778]"}`;
    }
    if (inboundLockEl) inboundLockEl.checked = Boolean(tunnel.tunnel_only_inbound);
    if (proxyHeadersEl) proxyHeadersEl.checked = Boolean(tunnel.proxy_headers);
    if (warpToggleEl && !isTogglingWarp) warpToggleEl.checked = warpEnabled;

    const enabledNow = Boolean(tunnel.cf_tunnel_enabled);
    initialState = {
      tunnel: {
        cf_tunnel_enabled: enabledNow,
        cf_tunnel_mode: loadedMode,
        cf_tunnel_token: hasSavedToken ? "••••••••••••" : "",
        tunnel_only_inbound: Boolean(tunnel.tunnel_only_inbound),
        proxy_headers: Boolean(tunnel.proxy_headers),
        warp_outbound_enabled: warpEnabled,
      },
    };

    if (!hasTelemetry) {
      syncToggleButton(enabledNow);
    }
    syncModeInputs();
  }

  function renderTelemetry(telemetry) {
    if (!telemetry) return;
    hasTelemetry = true;

    const badgeEl = document.getElementById("cf-status-badge");
    const outboundEl = document.getElementById("cf-outbound-badge");
    const outboundDotEl = document.getElementById("cf-outbound-dot");
    const outboundTextEl = document.getElementById("cf-outbound-text");
    const warpStatePillEl = document.getElementById("cf-warp-state-pill");
    const warpRamBadgeEl = document.getElementById("cf-warp-ram-badge");
    const warpEndpointEl = document.getElementById("cf-warp-endpoint");
    const warpDetailEl = document.getElementById("cf-warp-detail");
    const warpDiagnosticEl = document.getElementById("cf-warp-diagnostic");
    const warpToggleEl = document.getElementById("cf-warp-outbound-toggle");
    const originEl = document.getElementById("cf-origin-url");
    const urlWrapEl = document.getElementById("cf-public-url-wrap");
    const urlLinkEl = document.getElementById("cf-public-url");
    const errEl = document.getElementById("cf-last-error");

    if (badgeEl) {
      let label = "⚫ Đã tắt";
      let cls = "bg-[#181d27] text-[#8b95a7] border-[#2a3140]";
      if (telemetry.running) {
        const modeLabel = telemetry.mode === "quick" ? "Quick" : "Named";
        label = `🟢 Đang chạy (${modeLabel} · PID ${telemetry.pid ?? "?"})`;
        cls = "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
      } else if (telemetry.last_error) {
        label = "🔴 Lỗi Tunnel";
        cls = "bg-rose-500/15 text-rose-300 border-rose-500/30";
      } else if (telemetry.enabled) {
        label = "🟡 Đang khởi động...";
        cls = "bg-amber-500/15 text-amber-300 border-amber-500/30";
      }
      if (badgeEl.textContent !== label) badgeEl.textContent = label;
      badgeEl.className = `text-[11px] font-medium px-2.5 py-0.5 rounded-full border transition-colors duration-200 ${cls}`;
    }

    const warpInfo = telemetry.warp || {};
    const warpActive = Boolean(
      telemetry.warp_active ?? warpInfo.active ?? telemetry.outbound_mode === "proxy"
    );
    const warpEnabled = Boolean(
      telemetry.warp_enabled ?? warpInfo.enabled ?? initialState?.tunnel?.warp_outbound_enabled ?? true
    );
    const warpFallback = Boolean(
      telemetry.warp_fallback_direct ?? warpInfo.fallback_direct ?? (warpEnabled && !warpActive)
    );
    const warpRamMb = telemetry.warp_ram_mb ?? warpInfo.warp_ram_mb ?? null;
    const warpPid = telemetry.warp_pid ?? warpInfo.warp_pid ?? null;
    const warpProxyUrl = telemetry.warp_proxy_url || warpInfo.proxy_url || "socks5h://127.0.0.1:40000";
    const warpDiagnostic = String(
      telemetry.warp_diagnostic ?? warpInfo.diagnostic ?? warpInfo.last_error ?? ""
    ).trim();
    const warpTrace = telemetry.warp_trace ?? warpInfo.trace ?? null;
    renderWarpTrace(warpEnabled ? warpTrace : null);

    if (outboundEl) {
      const nextText = warpActive ? "Outbound: Cloudflare WARP" : "Outbound: Direct";
      if (outboundTextEl) {
        if (outboundTextEl.textContent !== nextText) outboundTextEl.textContent = nextText;
      } else if (outboundEl.textContent !== nextText) {
        outboundEl.textContent = nextText;
      }
      if (outboundDotEl) {
        outboundDotEl.classList.toggle("hidden", !warpActive);
      }
      outboundEl.className = `inline-flex items-center gap-1.5 text-[10px] font-mono px-2.5 py-0.5 rounded-full border transition-colors duration-200 ${
        warpActive
          ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
          : "bg-[#181d27] text-[#8b95a7] border-[#2a3140]"
      }`;
    }

    if (warpStatePillEl) {
      let pillText = "Đã tắt (Direct)";
      let pillCls = "bg-[#181d27] text-[#8b95a7] border-[#2a3140]";
      if (warpActive) {
        pillText = "WARP Kết nối";
        pillCls = "bg-emerald-500/15 text-emerald-300 border-emerald-500/30";
      } else if (warpFallback) {
        pillText = "Fallback Direct";
        pillCls = "bg-amber-500/15 text-amber-300 border-amber-500/30";
      }
      if (warpStatePillEl.textContent !== pillText) warpStatePillEl.textContent = pillText;
      warpStatePillEl.className = `text-[10px] font-mono px-1.5 py-0.5 rounded border transition-colors duration-200 ${pillCls}`;
    }

    if (warpRamBadgeEl) {
      const ramText =
        warpRamMb !== null && warpRamMb !== undefined
          ? `WARP RAM: ${warpRamMb} MB${warpPid ? ` · PID ${warpPid}` : ""}`
          : "WARP RAM: —";
      if (warpRamBadgeEl.textContent !== ramText) warpRamBadgeEl.textContent = ramText;
    }

    if (warpEndpointEl && warpEndpointEl.textContent !== warpProxyUrl) {
      warpEndpointEl.textContent = warpProxyUrl;
    }

    if (warpDetailEl) {
      let detailText = "Đã tắt WARP Outbound — Gemini & Discord dùng Direct · MongoDB Atlas: Direct TLS";
      if (warpActive) {
        const ramPart = warpRamMb !== null && warpRamMb !== undefined ? ` (${warpRamMb} MB RAM)` : "";
        detailText = `Gemini API & Discord đang đi qua Cloudflare WARP${ramPart} · MongoDB Atlas: Direct TLS`;
      } else if (warpFallback) {
        detailText = "Đang tạm dùng Direct fallback cho Gemini & Discord · MongoDB Atlas: Direct TLS";
      }
      if (warpDetailEl.textContent !== detailText) warpDetailEl.textContent = detailText;
    }

    if (warpDiagnosticEl) {
      const showDiag = !warpActive && warpEnabled && Boolean(warpDiagnostic);
      const diagText = showDiag ? `⚠️ ${warpDiagnostic}` : "";
      if (warpDiagnosticEl.textContent !== diagText) warpDiagnosticEl.textContent = diagText;
      warpDiagnosticEl.classList.toggle("hidden", !showDiag);
    }

    if (warpToggleEl && !isTogglingWarp && initialState?.tunnel) {
      warpToggleEl.checked = warpEnabled;
      initialState.tunnel.warp_outbound_enabled = warpEnabled;
    }

    if (telemetry.origin_url) {
      const originUrl = normalizeOriginUrl(telemetry.origin_url);
      if (originEl && originEl.textContent !== originUrl) {
        originEl.textContent = originUrl;
      }
      const namedOriginEl = document.getElementById("cf-named-origin-code");
      if (namedOriginEl && namedOriginEl.textContent !== originUrl) {
        namedOriginEl.textContent = originUrl;
      }
    }

    if (urlWrapEl && urlLinkEl) {
      const pubUrl = sanitizeHttpUrl(telemetry.public_url);
      if (pubUrl) {
        urlWrapEl.classList.remove("hidden");
        if (urlLinkEl.getAttribute("href") !== pubUrl) {
          urlLinkEl.setAttribute("href", pubUrl);
          urlLinkEl.textContent = pubUrl;
        }
      } else {
        urlWrapEl.classList.add("hidden");
        urlLinkEl.setAttribute("href", "#");
        urlLinkEl.textContent = "—";
      }
    }

    if (errEl) {
      const errText = telemetry.last_error ? `⚠️ ${telemetry.last_error}` : "";
      if (errEl.textContent !== errText) errEl.textContent = errText;
      errEl.classList.toggle("hidden", !errText);
    }

    // Đồng bộ chính xác theo bảng chân trị: running === true -> đỏ Tắt Tunnel; running === false -> xanh Bật Tunnel
    syncToggleButton(Boolean(telemetry.running));
  }

  function renderWarpTrace(trace) {
    const traceEl = document.getElementById("cf-warp-trace");
    if (!traceEl) return;
    if (!trace || typeof trace !== "object") {
      traceEl.textContent = "";
      traceEl.classList.add("hidden");
      return;
    }
    const warpState = String(trace.warp || "unknown");
    const parts = [`trace: warp=${warpState}`];
    if (trace.colo) parts.push(`colo=${trace.colo}`);
    if (trace.loc) parts.push(`loc=${trace.loc}`);
    if (trace.error) parts.push(`⚠️ ${trace.error}`);
    const text = `${trace.warp_ok ? "✅" : "❔"} ${parts.join(" · ")}`;
    if (traceEl.textContent !== text) traceEl.textContent = text;
    traceEl.classList.toggle("text-emerald-300", Boolean(trace.warp_ok));
    traceEl.classList.toggle("text-slate-400", !trace.warp_ok);
    traceEl.classList.remove("hidden");
  }

  async function submitWarpTrace() {
    const btn = document.getElementById("btn-warp-trace");
    if (btn) btn.disabled = true;
    try {
      const res = await apiRequest("/api/warp/trace", { method: "POST" });
      renderWarpTrace(res.trace);
      if (res.ok) {
        showToast(`✅ WARP xác nhận warp=${res.trace?.warp || "on"}${res.trace?.colo ? ` (${res.trace.colo})` : ""}`);
      } else {
        showToast(`⚠️ Trace chưa xác nhận WARP: ${res.trace?.error || res.trace?.warp || "không rõ"}`, false);
      }
    } catch {
      showToast("⚠️ Không kiểm tra được cdn-cgi/trace qua WARP.", false);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function submitWarpToggle(nextEnabled) {
    const warpToggleEl = document.getElementById("cf-warp-outbound-toggle");
    const prevEnabled = Boolean(initialState?.tunnel?.warp_outbound_enabled ?? !nextEnabled);

    isTogglingWarp = true;
    try {
      const outcome = await confirmSaveChanges({
        title: nextEnabled
          ? "Xác nhận bật Outbound qua Cloudflare WARP"
          : "Xác nhận tắt Outbound Cloudflare WARP",
        subtitle: nextEnabled
          ? "Hệ thống sẽ tạo hồ sơ wgcf (nếu thiếu), khởi chạy wireproxy tại 127.0.0.1 và kiểm tra kết nối trong tối đa ~7 giây"
          : "Định tuyến Gemini API & Discord sẽ chuyển ngay về kết nối Direct (MongoDB Atlas luôn giữ Direct TLS)",
        before: { tunnel: { warp_outbound_enabled: prevEnabled } },
        after: { tunnel: { warp_outbound_enabled: Boolean(nextEnabled) } },
        confirmLabel: nextEnabled ? "Bật WARP Outbound" : "Chuyển về Direct",
        confirmTone: nextEnabled ? "success" : "danger",
        onConfirm: async (changes) => {
          const endpoint = nextEnabled ? "/api/warp/start" : "/api/warp/stop";
          const res = await apiRequest(endpoint, {
            method: "POST",
            body: JSON.stringify({
              warp_outbound_enabled: Boolean(nextEnabled),
              changes,
            }),
          });
          if (res.config) {
            fill(res.config);
          }
          if (res.cloudflare_tunnel) {
            renderTelemetry(res.cloudflare_tunnel);
          }
          const warpActive = Boolean(res.warp?.active ?? res.cloudflare_tunnel?.warp_active);
          const diag = String(
            res.warp?.diagnostic ?? res.cloudflare_tunnel?.warp_diagnostic ?? ""
          ).trim();
          if (!nextEnabled) {
            showToast("⏹ Đã tắt Cloudflare WARP Outbound — chuyển sang kết nối Direct.");
          } else if (warpActive) {
            showToast(`⚡ Đã bật Outbound qua Cloudflare WARP (${res.warp?.proxy_url || "127.0.0.1:40000"})!`);
          } else {
            showToast(
              `⚠️ ${diag || "WARP proxy chưa phản hồi"} — tạm dùng Direct fallback.`,
              false
            );
          }
          if (onConfigSaved) await onConfigSaved();
        },
      });
      if (!outcome?.confirmed && warpToggleEl) {
        warpToggleEl.checked = prevEnabled;
      }
    } catch {
      if (warpToggleEl) warpToggleEl.checked = prevEnabled;
    } finally {
      isTogglingWarp = false;
    }
  }

  async function submitWarpReconnect() {
    isTogglingWarp = true;
    try {
      await showConfirmModal({
        title: "Khởi động lại & kiểm tra Cloudflare WARP",
        subtitle: "Sinh lại wireproxy.conf từ hồ sơ wgcf, khởi động lại wireproxy tại 127.0.0.1 và chờ cổng lắng nghe (~7 giây)",
        confirmLabel: "Kết nối lại WARP",
        confirmTone: "primary",
        entries: [
          {
            key: "tunnel.warp_outbound_enabled",
            label: "Định tuyến Outbound Cloudflare WARP",
            before: "Kiểm tra / cấu hình lại",
            after: "wireproxy SOCKS5 (127.0.0.1) + trace",
            formattedBefore: "Hiện tại",
            formattedAfter: "Khởi động & kết nối lại",
            isSensitive: false,
            isHighImpact: false,
            isLongText: false,
          },
        ],
        onConfirm: async () => {
          const res = await apiRequest("/api/warp/update", {
            method: "POST",
            body: JSON.stringify({ warp_outbound_enabled: true }),
          });
          if (res.config) {
            fill(res.config);
          }
          if (res.cloudflare_tunnel) {
            renderTelemetry(res.cloudflare_tunnel);
          }
          const warpActive = Boolean(res.warp?.active ?? res.cloudflare_tunnel?.warp_active);
          const diag = String(
            res.warp?.diagnostic ?? res.cloudflare_tunnel?.warp_diagnostic ?? ""
          ).trim();
          if (warpActive) {
            showToast(`⚡ Cloudflare WARP đã kết nối tại ${res.warp?.proxy_url || "127.0.0.1:40000"}!`);
          } else {
            showToast(`⚠️ ${diag || "WARP chưa kết nối được"} — đang giữ Direct fallback.`, false);
          }
          if (onConfigSaved) await onConfigSaved();
        },
      });
    } finally {
      isTogglingWarp = false;
    }
  }

  /**
   * Mở Hộp thoại Xác nhận trong ứng dụng bắt buộc cho cả 3 thao tác: `start` (Bật), `stop` (Tắt), `update` (Cập nhật).
   */
  async function submitTunnelAction(action) {
    const modeEl = document.getElementById("cf-tunnel-mode");
    const tokenEl = document.getElementById("cf-tunnel-token");
    const inboundLockEl = document.getElementById("cf-tunnel-only-inbound");
    const proxyHeadersEl = document.getElementById("cf-proxy-headers");
    const warpToggleEl = document.getElementById("cf-warp-outbound-toggle");

    const mode = modeEl ? modeEl.value : "named";
    const tokenVal = tokenEl ? tokenEl.value.trim() : "";
    const tunnelOnlyInbound = Boolean(inboundLockEl && inboundLockEl.checked);
    const proxyHeaders = Boolean(proxyHeadersEl && proxyHeadersEl.checked);
    const warpOutboundEnabled = warpToggleEl
      ? Boolean(warpToggleEl.checked)
      : Boolean(initialState?.tunnel?.warp_outbound_enabled ?? true);
    const enabled = action !== "stop";

    if (enabled && mode === "named" && !tokenVal && !hasSavedToken) {
      showToast("Vui lòng nhập Cloudflare Tunnel Token trước khi bật hoặc cập nhật chế độ Named Tunnel.", false);
      return;
    }

    const beforeState = initialState || {
      tunnel: {
        cf_tunnel_enabled: isTunnelActive,
        cf_tunnel_mode: "named",
        cf_tunnel_token: hasSavedToken ? "••••••••••••" : "",
        tunnel_only_inbound: false,
        proxy_headers: false,
        warp_outbound_enabled: true,
      },
    };

    // Dùng chuỗi che `••••••••` khi người dùng nhập token mới để đảm bảo diff nhận diện thay đổi mà không lộ bí mật
    const effectiveTokenAfter = tokenVal
      ? "••••••••"
      : beforeState.tunnel.cf_tunnel_token;

    const nextState = {
      tunnel: {
        cf_tunnel_enabled: enabled,
        cf_tunnel_mode: mode,
        cf_tunnel_token: effectiveTokenAfter,
        tunnel_only_inbound: enabled ? tunnelOnlyInbound : false,
        proxy_headers: proxyHeaders,
        warp_outbound_enabled: warpOutboundEnabled,
      },
    };

    const customWarnings = [];
    if (action === "stop") {
      customWarnings.push(
        "Dừng tiến trình cloudflared sẽ ngắt kết nối ngay lập tức tên miền công khai (kiwikiwi.dpdns.org) cho đến khi bạn bật lại Tunnel."
      );
    }

    const { entries, changesMap, warnings } = computeFormDiff(beforeState, nextState, {
      customWarnings,
    });

    // Bảo đảm luôn hiển thị rõ dòng tóm tắt thao tác trong Modal cho cả 3 hành động (kể cả khi reload nóng không đổi trường)
    const modalEntries = [...entries];
    if (action === "start" && !modalEntries.some((row) => row.key === "tunnel.cf_tunnel_mode")) {
      modalEntries.push({
        key: "tunnel.cf_tunnel_mode",
        label: "Chế độ khởi chạy Cloudflare Tunnel",
        before: beforeState.tunnel.cf_tunnel_mode,
        after: mode,
        formattedBefore: formatModeLabel(beforeState.tunnel.cf_tunnel_mode),
        formattedAfter: formatModeLabel(mode),
        isSensitive: false,
        isHighImpact: false,
        isLongText: false,
      });
    } else if (action === "update" && modalEntries.length === 0) {
      modalEntries.push({
        key: "tunnel.reload",
        label: "Áp dụng & khởi động lại nóng cloudflared",
        before: isTunnelActive ? "Đang chạy" : "Đang tắt",
        after: `Khởi động lại (${formatModeLabel(mode)})`,
        formattedBefore: isTunnelActive ? "Đang chạy" : "Đang tắt",
        formattedAfter: `Khởi động lại nóng (${formatModeLabel(mode)})`,
        isSensitive: false,
        isHighImpact: false,
        isLongText: false,
      });
    } else if (action === "stop" && modalEntries.length === 0) {
      modalEntries.push({
        key: "tunnel.cf_tunnel_enabled",
        label: "Trạng thái Cloudflare Tunnel",
        before: true,
        after: false,
        formattedBefore: "Bật",
        formattedAfter: "Tắt",
        isSensitive: false,
        isHighImpact: true,
        isLongText: false,
      });
    }

    const modalMeta =
      action === "stop"
        ? {
            title: "Xác nhận tắt Cloudflare Tunnel",
            subtitle: "Tiến trình cloudflared sẽ dừng ngay lập tức và lưu trạng thái tắt vào MongoDB Atlas",
            confirmLabel: "Tắt Tunnel",
            confirmTone: "danger",
          }
        : action === "start"
          ? {
              title: "Xác nhận bật Cloudflare Tunnel",
              subtitle: `Khởi chạy tiến trình cloudflared ở chế độ ${formatModeLabel(mode)} không cần khởi động lại bot`,
              confirmLabel: "Bật Tunnel",
              confirmTone: "success",
            }
          : {
              title: "Xác nhận cập nhật Cloudflare Tunnel",
              subtitle: "Lưu cấu hình Chế độ / Token mới và khởi động lại nóng tiến trình cloudflared",
              confirmLabel: "Cập nhật Tunnel",
              confirmTone: "primary",
            };

    await showConfirmModal({
      ...modalMeta,
      warnings,
      entries: modalEntries,
      changes: changesMap,
      onConfirm: async (changes) => {
        const res = await apiRequest("/api/config/tunnel", {
          method: "PUT",
          body: JSON.stringify({
            cf_tunnel_enabled: enabled,
            cf_tunnel_mode: mode,
            cf_tunnel_token: tokenVal || null,
            tunnel_only_inbound: enabled ? tunnelOnlyInbound : false,
            proxy_headers: proxyHeaders,
            warp_outbound_enabled: warpOutboundEnabled,
            changes,
          }),
        });
        if (res.config) {
          fill(res.config);
        }
        if (res.cloudflare_tunnel) {
          renderTelemetry(res.cloudflare_tunnel);
        } else {
          syncToggleButton(enabled);
        }

        if (action === "stop") {
          showToast("Đã tắt Cloudflare Tunnel và lưu cấu hình vào MongoDB Atlas.");
        } else if (res.cloudflare_tunnel?.last_error) {
          showToast(`Đã lưu cấu hình nhưng cloudflared báo lỗi: ${res.cloudflare_tunnel.last_error}`, false);
        } else if (res.cloudflare_tunnel?.public_url) {
          showToast(`✅ Cloudflare Quick Tunnel đã chạy: ${res.cloudflare_tunnel.public_url}`);
        } else if (action === "start") {
          showToast(`✅ Đã bật Cloudflare Tunnel (${formatModeLabel(mode)}) và lưu MongoDB Atlas!`);
        } else {
          showToast("✅ Đã cập nhật cấu hình và khởi động lại nóng Cloudflare Tunnel!");
        }
        if (onConfigSaved) await onConfigSaved();
      },
    });
  }

  async function copyOriginToClipboard() {
    const originEl = document.getElementById("cf-origin-url");
    const originText = normalizeOriginUrl(originEl?.textContent || "");
    if (!originText) return;

    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(originText);
      } else {
        const tempInput = document.createElement("textarea");
        tempInput.value = originText;
        tempInput.setAttribute("readonly", "");
        tempInput.style.position = "fixed";
        tempInput.style.opacity = "0";
        document.body.appendChild(tempInput);
        tempInput.select();
        document.execCommand("copy");
        document.body.removeChild(tempInput);
      }
      const copyIcon = document.getElementById("cf-copy-icon");
      const checkIcon = document.getElementById("cf-copy-check-icon");
      if (copyIcon && checkIcon) {
        copyIcon.classList.add("hidden");
        checkIcon.classList.remove("hidden");
        clearTimeout(copyOriginToClipboard._timerId);
        copyOriginToClipboard._timerId = setTimeout(() => {
          checkIcon.classList.add("hidden");
          copyIcon.classList.remove("hidden");
        }, 1800);
      }
      showToast(`✅ Đã sao chép Origin: ${originText}`);
    } catch {
      showToast("Không thể sao chép tự động, vui lòng bôi đen địa chỉ Origin để sao chép.", false);
    }
  }

  function bindEvents() {
    const modeEl = document.getElementById("cf-tunnel-mode");
    if (modeEl) modeEl.addEventListener("change", syncModeInputs);

    const toggleBtn = document.getElementById("btn-toggle-tunnel");
    if (toggleBtn) {
      // Đồng bộ trạng thái khởi tạo từ HTML render phía server trước khi nhận telemetry đầu tiên
      syncToggleButton(toggleBtn.dataset.tunnelActive === "true");
    }

    // Ủy quyền sự kiện click/change trên khung thẻ Tunnel để không bao giờ mất listener khi nút đổi trạng thái động
    const tunnelCardEl = document.getElementById("cf-tunnel-card") || document;
    tunnelCardEl.addEventListener("click", (event) => {
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;

      const clickedToggle = target.closest("#btn-toggle-tunnel");
      if (clickedToggle && !clickedToggle.disabled) {
        event.preventDefault();
        submitTunnelAction(isTunnelActive ? "stop" : "start");
        return;
      }

      const clickedSave = target.closest("#btn-save-tunnel");
      if (clickedSave && !clickedSave.disabled) {
        event.preventDefault();
        submitTunnelAction("update");
        return;
      }

      const clickedCopy = target.closest("#btn-copy-cf-origin");
      if (clickedCopy) {
        event.preventDefault();
        copyOriginToClipboard();
        return;
      }

      const clickedWarpTrace = target.closest("#btn-warp-trace");
      if (clickedWarpTrace && !clickedWarpTrace.disabled && isAdmin) {
        event.preventDefault();
        submitWarpTrace();
        return;
      }

      const clickedWarpReconnect = target.closest("#btn-warp-reconnect");
      if (clickedWarpReconnect && !clickedWarpReconnect.disabled && isAdmin) {
        event.preventDefault();
        submitWarpReconnect();
      }
    });

    if (isAdmin) {
      tunnelCardEl.addEventListener("change", (event) => {
        const target = event.target;
        if (target && target.id === "cf-warp-outbound-toggle" && !target.disabled) {
          submitWarpToggle(Boolean(target.checked));
        }
      });
    }
  }

  return { fill, renderTelemetry, bindEvents };
}
