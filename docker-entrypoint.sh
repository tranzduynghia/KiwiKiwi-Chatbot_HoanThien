#!/usr/bin/env bash
# Non-blocking container entrypoint:
# - Starts Cloudflare WARP daemon (warp-svc) in user-space proxy mode on 127.0.0.1:<WARP_PROXY_PORT>
# - Enforces a strict readiness timeout (default 5s) so bot startup never hangs or crashes if WARP fails
# - Never prints secret values (WARP_LICENSE_KEY, WARP_CONNECTOR_TOKEN) to stdout/stderr

set -u

WARP_ENABLED_RAW="${WARP_ENABLED:-true}"
WARP_PROXY_PORT_RAW="${WARP_PROXY_PORT:-40000}"
WARP_TIMEOUT_RAW="${WARP_STARTUP_TIMEOUT:-5}"

# Validate numeric port & timeout safely (fallback to 40000 / 5s)
if [[ "${WARP_PROXY_PORT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_PROXY_PORT_RAW}" -ge 1 ] && [ "${WARP_PROXY_PORT_RAW}" -le 65535 ]; then
  WARP_PROXY_PORT="${WARP_PROXY_PORT_RAW}"
else
  WARP_PROXY_PORT=40000
fi

if [[ "${WARP_TIMEOUT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_TIMEOUT_RAW}" -ge 1 ] && [ "${WARP_TIMEOUT_RAW}" -le 15 ]; then
  WARP_STARTUP_TIMEOUT="${WARP_TIMEOUT_RAW}"
else
  WARP_STARTUP_TIMEOUT=5
fi

is_truthy() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    0|false|no|off|"") return 1 ;;
    *) return 0 ;;
  esac
}

probe_proxy_port() {
  python3 - "$1" <<'PY' >/dev/null 2>&1
import socket, sys
port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=0.25):
    pass
PY
}

start_warp_proxy() {
  if ! is_truthy "${WARP_ENABLED_RAW}"; then
    echo "[WARP] Disabled via WARP_ENABLED=${WARP_ENABLED_RAW}; booting with direct outbound."
    return 0
  fi

  if ! command -v warp-svc >/dev/null 2>&1 || ! command -v warp-cli >/dev/null 2>&1; then
    echo "[WARP] warp-svc/warp-cli not installed; booting with direct outbound."
    return 0
  fi

  local log_dir="/var/log/cloudflare-warp"
  local log_file="${log_dir}/warp-svc.log"
  if [ ! -w "${log_dir}" ]; then
    log_file="/tmp/warp-svc.log"
  fi

  local deadline=$(( $(date +%s) + WARP_STARTUP_TIMEOUT ))

  # Launch warp-svc in background without blocking PID 1
  warp-svc >"${log_file}" 2>&1 &
  local warp_pid=$!

  # Wait briefly for warp-svc IPC to respond within the startup budget
  local svc_ready=0
  while [ "$(date +%s)" -lt "${deadline}" ]; do
    if ! kill -0 "${warp_pid}" >/dev/null 2>&1; then
      echo "[WARP] warp-svc exited early (unprivileged/sandboxed host?); falling back to direct outbound."
      return 0
    fi
    if warp-cli --accept-tos status >/dev/null 2>&1; then
      svc_ready=1
      break
    fi
    sleep 0.25
  done

  if [ "${svc_ready}" -ne 1 ]; then
    echo "[WARP] warp-svc IPC not ready within ${WARP_STARTUP_TIMEOUT}s; continuing boot with direct fallback."
    return 0
  fi

  # Register non-interactively if no profile exists yet
  if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    if [ -n "${WARP_CONNECTOR_TOKEN:-}" ]; then
      warp-cli --accept-tos connector new "${WARP_CONNECTOR_TOKEN}" >/dev/null 2>&1 || true
    else
      warp-cli --accept-tos registration new >/dev/null 2>&1 || true
      if [ -n "${WARP_LICENSE_KEY:-}" ]; then
        warp-cli --accept-tos registration license "${WARP_LICENSE_KEY}" >/dev/null 2>&1 || true
      fi
    fi
  fi

  # Configure lightweight user-space proxy mode (no TUN device or NET_ADMIN required)
  warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
  warp-cli --accept-tos proxy port "${WARP_PROXY_PORT}" >/dev/null 2>&1 || true
  warp-cli --accept-tos connect >/dev/null 2>&1 || true

  # Short readiness check on 127.0.0.1:<WARP_PROXY_PORT> within remaining timeout budget
  local proxy_ready=0
  while [ "$(date +%s)" -le "${deadline}" ]; do
    if probe_proxy_port "${WARP_PROXY_PORT}"; then
      proxy_ready=1
      break
    fi
    sleep 0.25
  done

  if [ "${proxy_ready}" -eq 1 ]; then
    echo "[WARP] Cloudflare WARP user-space proxy ready at 127.0.0.1:${WARP_PROXY_PORT} (pid=${warp_pid})."
  else
    echo "[WARP] Proxy 127.0.0.1:${WARP_PROXY_PORT} not ready within ${WARP_STARTUP_TIMEOUT}s; continuing boot (Python will auto-detect or fall back to direct)."
  fi
  return 0
}

start_warp_proxy || true

exec "$@"
