#!/usr/bin/env bash
# Non-blocking container entrypoint:
# - Ensures WARP state/runtime directories are present and writable
# - Starts Cloudflare WARP daemon (warp-svc) in user-space proxy mode strictly bound to 127.0.0.1:<WARP_PROXY_PORT>
# - Uses bounded timeouts (IPC wait + short proxy readiness check) so bot startup never hangs or crashes
# - Never prints secret values (WARP_LICENSE_KEY, WARP_CONNECTOR_TOKEN) to stdout/stderr

set -u

WARP_ENABLED_RAW="${WARP_ENABLED:-true}"
WARP_PROXY_PORT_RAW="${WARP_PROXY_PORT:-40000}"
WARP_IPC_TIMEOUT_RAW="${WARP_STARTUP_TIMEOUT:-5}"
WARP_CONNECT_WAIT_RAW="${WARP_BOOT_CONNECT_WAIT:-4}"

# Validate numeric port & timeouts safely (loopback port 1..65535)
if [[ "${WARP_PROXY_PORT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_PROXY_PORT_RAW}" -ge 1 ] && [ "${WARP_PROXY_PORT_RAW}" -le 65535 ]; then
  WARP_PROXY_PORT="${WARP_PROXY_PORT_RAW}"
else
  WARP_PROXY_PORT=40000
fi

if [[ "${WARP_IPC_TIMEOUT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_IPC_TIMEOUT_RAW}" -ge 1 ] && [ "${WARP_IPC_TIMEOUT_RAW}" -le 15 ]; then
  WARP_IPC_TIMEOUT="${WARP_IPC_TIMEOUT_RAW}"
else
  WARP_IPC_TIMEOUT=5
fi

if [[ "${WARP_CONNECT_WAIT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_CONNECT_WAIT_RAW}" -ge 1 ] && [ "${WARP_CONNECT_WAIT_RAW}" -le 10 ]; then
  WARP_CONNECT_WAIT="${WARP_CONNECT_WAIT_RAW}"
else
  WARP_CONNECT_WAIT=4
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

tail_sanitized_log() {
  local logfile="$1"
  if [ -r "${logfile}" ]; then
    tail -n 3 "${logfile}" 2>/dev/null \
      | tr '\r\n' ' ' \
      | sed -E 's#(/[A-Za-z0-9._-]+){2,}#[path]#g; s/([Tt]oken|[Kk]ey|[Ss]ecret)[=:][^ ]+/\1=[REDACTED]/g' \
      | cut -c1-180
  fi
}

start_warp_proxy() {
  if ! is_truthy "${WARP_ENABLED_RAW}"; then
    echo "[WARP] Disabled via WARP_ENABLED=${WARP_ENABLED_RAW}; booting with direct outbound."
    return 0
  fi

  if ! command -v warp-svc >/dev/null 2>&1 || ! command -v warp-cli >/dev/null 2>&1; then
    echo "[WARP] cloudflare-warp (warp-svc/warp-cli) not installed; booting with direct outbound."
    return 0
  fi

  # Ensure runtime directories exist (handles fresh tmpfs /run on cloud hosts like Northflank)
  mkdir -p /var/lib/cloudflare-warp /run/cloudflare-warp /var/log/cloudflare-warp 2>/dev/null || true

  local log_dir="/var/log/cloudflare-warp"
  local log_file="${log_dir}/warp-svc.log"
  if [ ! -w "${log_dir}" ]; then
    log_file="/tmp/warp-svc.log"
  fi

  local warp_pid=""
  local svc_ready=0

  # Check if warp-svc IPC is already responsive before spawning a new daemon
  if warp-cli --accept-tos status >/dev/null 2>&1; then
    svc_ready=1
  else
    # Try launching warp-svc with --accept-tos first; fallback to plain warp-svc if flag unsupported
    warp-svc --accept-tos >>"${log_file}" 2>&1 &
    warp_pid=$!
    sleep 0.25
    if ! kill -0 "${warp_pid}" >/dev/null 2>&1; then
      if grep -qiE "unexpected argument|unrecognized|unknown option|accept-tos" "${log_file}" 2>/dev/null; then
        warp-svc >>"${log_file}" 2>&1 &
        warp_pid=$!
      fi
    fi

    # Wait up to WARP_IPC_TIMEOUT seconds (default 5s) for warp-svc IPC socket to accept commands
    local ipc_deadline=$(( $(date +%s) + WARP_IPC_TIMEOUT ))
    while [ "$(date +%s)" -lt "${ipc_deadline}" ]; do
      if ! kill -0 "${warp_pid}" >/dev/null 2>&1; then
        local reason
        reason="$(tail_sanitized_log "${log_file}")"
        echo "[WARP] warp-svc exited early (${reason:-no stderr}); falling back to direct outbound."
        return 0
      fi
      if warp-cli --accept-tos status >/dev/null 2>&1; then
        svc_ready=1
        break
      fi
      sleep 0.25
    done
  fi

  if [ "${svc_ready}" -ne 1 ]; then
    echo "[WARP] warp-svc IPC not ready within ${WARP_IPC_TIMEOUT}s; continuing boot (Python WARP manager will retry/fallback)."
    return 0
  fi

  # Register non-interactively if no profile exists yet (never echo license keys or tokens)
  if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    if [ -n "${WARP_CONNECTOR_TOKEN:-}" ]; then
      warp-cli --accept-tos connector new "${WARP_CONNECTOR_TOKEN}" >/dev/null 2>&1 || true
    else
      warp-cli --accept-tos registration new >/dev/null 2>&1 || true
      if [ -n "${WARP_LICENSE_KEY:-}" ]; then
        warp-cli --accept-tos registration license "${WARP_LICENSE_KEY}" >/dev/null 2>&1 || true
      fi
    fi
  elif [ -n "${WARP_LICENSE_KEY:-}" ]; then
    warp-cli --accept-tos registration license "${WARP_LICENSE_KEY}" >/dev/null 2>&1 || true
  fi

  # Force lightweight user-space proxy mode bound strictly to 127.0.0.1:<WARP_PROXY_PORT>
  warp-cli --accept-tos mode proxy >/dev/null 2>&1 || true
  warp-cli --accept-tos proxy port "${WARP_PROXY_PORT}" >/dev/null 2>&1 || true
  warp-cli --accept-tos connect >/dev/null 2>&1 || true

  # Dedicated short socket readiness poll on 127.0.0.1:<WARP_PROXY_PORT>
  local proxy_ready=0
  local connect_deadline=$(( $(date +%s) + WARP_CONNECT_WAIT ))
  while [ "$(date +%s)" -le "${connect_deadline}" ]; do
    if probe_proxy_port "${WARP_PROXY_PORT}"; then
      proxy_ready=1
      break
    fi
    sleep 0.5
  done

  if [ "${proxy_ready}" -eq 1 ]; then
    echo "[WARP] Cloudflare WARP user-space proxy ready at 127.0.0.1:${WARP_PROXY_PORT}${warp_pid:+ (pid=${warp_pid})}."
  else
    echo "[WARP] Proxy 127.0.0.1:${WARP_PROXY_PORT} still warming up after ${WARP_CONNECT_WAIT}s; continuing boot (Python will auto-detect or fall back to direct)."
  fi
  return 0
}

start_warp_proxy || true

exec "$@"
