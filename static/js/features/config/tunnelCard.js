/**
 * Bộ điều khiển thẻ "Cloudflare Tunnel (cloudflared)":
 * - Chọn chế độ Named Tunnel (Token) hoặc Quick Tunnel (trycloudflare.com)
 * - Nhập Token ẩn mật khẩu, bật/tắt khóa "Chỉ nhận truy cập qua Cloudflare Tunnel"
 * - Bật / Cập nhật nóng và Tắt Tunnel tức thì qua PUT /api/config/tunnel không cần restart bot
 * - Đồng bộ im lặng trạng thái chạy, PID, Public URL có thể bấm và chế độ Outbound Proxy
 */

import { apiRequest, sanitizeHttpUrl, showToast } from "../../core/api.js";
import { confirmSaveChanges } from "../../core/confirmSave.js";

export function createTunnelCardController({ isAdmin, onConfigSaved }) {
  let hasSavedToken = false;
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

  function fill(config) {
    const tunnel = config?.tunnel || {};
    hasSavedToken = Boolean(tunnel.has_cf_tunnel_token ?? tunnel.cf_tunnel_has_token);

    const modeEl = document.getElementById("cf-tunnel-mode");
    const tokenEl = document.getElementById("cf-tunnel-token");
    const tokenHintEl = document.getElementById("cf-token-hint");
    const inboundLockEl = document.getElementById("cf-tunnel-only-inbound");

    const loadedMode = tunnel.cf_tunnel_mode === "quick" ? "quick" : "named";
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

    initialState = {
      tunnel: {
        cf_tunnel_enabled: Boolean(tunnel.cf_tunnel_enabled),
        cf_tunnel_mode: loadedMode,
        cf_tunnel_token: hasSavedToken ? "••••••••••••" : "",
        tunnel_only_inbound: Boolean(tunnel.tunnel_only_inbound),
      },
    };

    syncModeInputs();
  }

  function renderTelemetry(telemetry) {
    if (!telemetry) return;

    const badgeEl = document.getElementById("cf-status-badge");
    const outboundEl = document.getElementById("cf-outbound-badge");
    const originEl = document.getElementById("cf-origin-url");
    const urlWrapEl = document.getElementById("cf-public-url-wrap");
    const urlLinkEl = document.getElementById("cf-public-url");
    const errEl = document.getElementById("cf-last-error");
    const stopBtn = document.getElementById("btn-stop-tunnel");

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

    if (outboundEl) {
      const isProxy = telemetry.outbound_mode === "proxy";
      const nextText = isProxy ? "Outbound: Proxy (WARP)" : "Outbound: Direct";
      if (outboundEl.textContent !== nextText) outboundEl.textContent = nextText;
      outboundEl.className = `text-[10px] font-mono px-2 py-0.5 rounded-full border ${
        isProxy
          ? "bg-sky-500/15 text-sky-300 border-sky-500/30"
          : "bg-[#181d27] text-[#8b95a7] border-[#2a3140]"
      }`;
    }

    if (originEl && telemetry.origin_url) {
      const nextOrigin = `Origin nội bộ: ${telemetry.origin_url}`;
      if (originEl.textContent !== nextOrigin) originEl.textContent = nextOrigin;
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

    if (stopBtn && isAdmin) {
      stopBtn.disabled = !telemetry.running && !telemetry.enabled;
    }
  }

  async function submitTunnelUpdate(enabled) {
    const modeEl = document.getElementById("cf-tunnel-mode");
    const tokenEl = document.getElementById("cf-tunnel-token");
    const inboundLockEl = document.getElementById("cf-tunnel-only-inbound");

    const mode = modeEl ? modeEl.value : "named";
    const tokenVal = tokenEl ? tokenEl.value.trim() : "";
    const tunnelOnlyInbound = Boolean(inboundLockEl && inboundLockEl.checked);

    if (enabled && mode === "named" && !tokenVal && !hasSavedToken) {
      showToast("Vui lòng nhập Cloudflare Tunnel Token trước khi bật chế độ Named Tunnel.", false);
      return;
    }

    const beforeState = initialState || {
      tunnel: {
        cf_tunnel_enabled: false,
        cf_tunnel_mode: "named",
        cf_tunnel_token: "",
        tunnel_only_inbound: false,
      },
    };
    const nextState = {
      tunnel: {
        cf_tunnel_enabled: Boolean(enabled),
        cf_tunnel_mode: mode,
        cf_tunnel_token: tokenVal || beforeState.tunnel.cf_tunnel_token,
        tunnel_only_inbound: enabled ? tunnelOnlyInbound : false,
      },
    };

    await confirmSaveChanges({
      title: enabled ? "Xác nhận bật / cập nhật Cloudflare Tunnel" : "Xác nhận tắt Cloudflare Tunnel",
      subtitle: "Tiến trình cloudflared sẽ được áp dụng nóng ngay lập tức mà không cần khởi động lại bot",
      before: beforeState,
      after: nextState,
      confirmLabel: enabled ? "Xác nhận áp dụng" : "Xác nhận tắt Tunnel",
      onConfirm: async (changes) => {
        const res = await apiRequest("/api/config/tunnel", {
          method: "PUT",
          body: JSON.stringify({
            cf_tunnel_enabled: enabled,
            cf_tunnel_mode: mode,
            cf_tunnel_token: tokenVal || null,
            tunnel_only_inbound: enabled ? tunnelOnlyInbound : false,
            changes,
          }),
        });
        if (res.cloudflare_tunnel) {
          renderTelemetry(res.cloudflare_tunnel);
        }
        if (res.config) {
          fill(res.config);
        }
        if (!enabled) {
          showToast("⏹ Đã tắt Cloudflare Tunnel và lưu cấu hình vào MongoDB Atlas.");
        } else if (res.cloudflare_tunnel?.last_error) {
          showToast(`Đã lưu cấu hình nhưng cloudflared báo lỗi: ${res.cloudflare_tunnel.last_error}`, false);
        } else if (res.cloudflare_tunnel?.public_url) {
          showToast(`✅ Cloudflare Quick Tunnel đã chạy: ${res.cloudflare_tunnel.public_url}`);
        } else {
          showToast("✅ Đã bật / cập nhật nóng Cloudflare Tunnel và lưu MongoDB Atlas!");
        }
        if (onConfigSaved) await onConfigSaved();
      },
    });
  }

  function bindEvents() {
    const modeEl = document.getElementById("cf-tunnel-mode");
    if (modeEl) modeEl.addEventListener("change", syncModeInputs);

    const saveBtn = document.getElementById("btn-save-tunnel");
    const stopBtn = document.getElementById("btn-stop-tunnel");
    if (saveBtn) {
      saveBtn.addEventListener("click", () => submitTunnelUpdate(true));
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", () => submitTunnelUpdate(false));
    }
  }

  return { fill, renderTelemetry, bindEvents };
}
