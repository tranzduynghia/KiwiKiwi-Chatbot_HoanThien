#!/usr/bin/env bash
# Non-blocking container entrypoint:
# - Ensures the WARP state directory (/var/lib/warp-proxy) exists and is writable
# - Registers a free Cloudflare WARP account with static `wgcf` (optional WARP+ via WGCF_LICENSE_KEY) and generates a WireGuard profile
# - Renders wireproxy.conf with a SOCKS5 listener strictly bound to 127.0.0.1:<WARP_PROXY_PORT> (never 0.0.0.0)
# - Starts `wireproxy` (user-space WireGuard, no root / TUN / DBus / systemd) in the background with a bounded readiness check
# - Never prints secret values (WARP_LICENSE_KEY, WireGuard private keys) to stdout/stderr

set -u

WARP_ENABLED_RAW="${WARP_ENABLED:-true}"
WARP_PROXY_PORT_RAW="${WARP_PROXY_PORT:-40000}"
WARP_HTTP_PROXY_PORT_RAW="${WARP_HTTP_PROXY_PORT:-}"
WARP_STARTUP_TIMEOUT_RAW="${WARP_STARTUP_TIMEOUT:-5}"
WARP_STATE_DIR="${WARP_STATE_DIR:-/var/lib/warp-proxy}"
WGCF_TIMEOUT=25

# Validate numeric ports & timeout safely (loopback port 1..65535)
if [[ "${WARP_PROXY_PORT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_PROXY_PORT_RAW}" -ge 1 ] && [ "${WARP_PROXY_PORT_RAW}" -le 65535 ]; then
  WARP_PROXY_PORT="${WARP_PROXY_PORT_RAW}"
else
  WARP_PROXY_PORT=40000
fi

WARP_HTTP_PROXY_PORT=""
if [[ "${WARP_HTTP_PROXY_PORT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_HTTP_PROXY_PORT_RAW}" -ge 1 ] && [ "${WARP_HTTP_PROXY_PORT_RAW}" -le 65535 ] && [ "${WARP_HTTP_PROXY_PORT_RAW}" != "${WARP_PROXY_PORT}" ]; then
  WARP_HTTP_PROXY_PORT="${WARP_HTTP_PROXY_PORT_RAW}"
fi

if [[ "${WARP_STARTUP_TIMEOUT_RAW}" =~ ^[0-9]+$ ]] && [ "${WARP_STARTUP_TIMEOUT_RAW}" -ge 1 ] && [ "${WARP_STARTUP_TIMEOUT_RAW}" -le 15 ]; then
  WARP_STARTUP_TIMEOUT="${WARP_STARTUP_TIMEOUT_RAW}"
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

sanitize_line() {
  tr '\r\n' '  ' \
    | sed -E 's#(/[A-Za-z0-9._-]+){2,}#[path]#g; s/([Pp]rivate_?[Kk]ey|[Ll]icense_?[Kk]ey|[Tt]oken|[Ss]ecret)[ ]*[=:][ ]*[^ ]+/\1=[REDACTED]/g; s/[A-Za-z0-9+\/]{42,43}=/[REDACTED]/g; s/\b([0-9]{1,3}\.){3}[0-9]{1,3}\b/[ip]/g' \
    | cut -c1-180
}

tail_sanitized_log() {
  local logfile="$1"
  if [ -r "${logfile}" ]; then
    grep -v '^DEBUG' "${logfile}" 2>/dev/null | tail -n 2 | sanitize_line
  fi
}

run_wgcf() {
  # Runs wgcf inside the state dir with a bounded timeout; output is captured (never echoed raw)
  local out
  out="$(cd "${WARP_STATE_DIR}" && timeout "${WGCF_TIMEOUT}" wgcf "$@" 2>&1)"
  local rc=$?
  if [ "${rc}" -ne 0 ]; then
    printf '%s' "${out}" | grep -iE 'error|fail|refused|timeout|unable' | tail -n 1 | sanitize_line
  fi
  return "${rc}"
}

render_wireproxy_config() {
  python3 - "${WARP_STATE_DIR}/wgcf-profile.conf" "${WARP_STATE_DIR}/wireproxy.conf" "${WARP_PROXY_PORT}" "${WARP_HTTP_PROXY_PORT}" <<'PY'
import configparser, os, sys
src, dst, socks_port, http_port = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
cp = configparser.ConfigParser(interpolation=None, strict=False)
cp.optionxform = str
cp.read(src)
iface, peer = cp["Interface"], cp["Peer"]
for key, sec in (("PrivateKey", iface), ("Address", iface), ("PublicKey", peer), ("Endpoint", peer)):
    if not sec.get(key):
        raise SystemExit(f"profile missing {key}")
lines = ["[Interface]"]
lines += [f"{k} = {iface[k]}" for k in ("PrivateKey", "Address", "DNS", "MTU") if iface.get(k)]
lines += ["", "[Peer]"]
lines += [f"{k} = {peer[k]}" for k in ("PublicKey", "AllowedIPs", "Endpoint", "PersistentKeepalive") if peer.get(k)]
lines += ["", "[Socks5]", f"BindAddress = 127.0.0.1:{socks_port}"]
if http_port:
    lines += ["", "[http]", f"BindAddress = 127.0.0.1:{int(http_port)}"]
tmp = dst + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fp:
    fp.write("\n".join(lines) + "\n")
os.replace(tmp, dst)
PY
}

start_warp_proxy() {
  if ! is_truthy "${WARP_ENABLED_RAW}"; then
    echo "[WARP] Disabled via WARP_ENABLED=${WARP_ENABLED_RAW}; booting with direct outbound."
    return 0
  fi

  if ! command -v wgcf >/dev/null 2>&1 || ! command -v wireproxy >/dev/null 2>&1; then
    echo "[WARP] wgcf/wireproxy not installed; booting with direct outbound."
    return 0
  fi

  if probe_proxy_port "${WARP_PROXY_PORT}"; then
    echo "[WARP] 127.0.0.1:${WARP_PROXY_PORT} already listening; skipping wireproxy bootstrap."
    return 0
  fi

  mkdir -p "${WARP_STATE_DIR}" 2>/dev/null || true
  if [ ! -w "${WARP_STATE_DIR}" ]; then
    echo "[WARP] State dir not writable; booting with direct outbound (Python WARP manager will retry)."
    return 0
  fi
  umask 077

  local regenerate=0
  local reason=""
  if [ ! -s "${WARP_STATE_DIR}/wgcf-account.toml" ]; then
    if ! reason="$(run_wgcf register --accept-tos)"; then
      echo "[WARP] wgcf register failed (${reason:-no detail}); booting with direct outbound."
      return 0
    fi
    regenerate=1
  fi

  local license_key="${WARP_LICENSE_KEY:-${WGCF_LICENSE_KEY:-}}"
  if [ -n "${license_key}" ]; then
    local digest
    digest="$(printf '%s' "${license_key}" | sha256sum | cut -d' ' -f1)"
    if [ "$(cat "${WARP_STATE_DIR}/.license-applied" 2>/dev/null)" != "${digest}" ]; then
      # License key is passed only to this single wgcf process via env, never echoed
      if reason="$(WGCF_LICENSE_KEY="${license_key}" run_wgcf update)"; then
        printf '%s' "${digest}" > "${WARP_STATE_DIR}/.license-applied"
        regenerate=1
      else
        echo "[WARP] WARP+ license not applied (${reason:-rejected}); continuing with free profile."
      fi
    fi
  fi
  unset license_key

  if [ "${regenerate}" -eq 1 ] || [ ! -s "${WARP_STATE_DIR}/wgcf-profile.conf" ]; then
    if ! reason="$(run_wgcf generate)"; then
      echo "[WARP] wgcf generate failed (${reason:-no detail}); booting with direct outbound."
      return 0
    fi
  fi

  if ! render_wireproxy_config >/dev/null 2>&1; then
    echo "[WARP] Could not render wireproxy.conf from WireGuard profile; booting with direct outbound."
    return 0
  fi

  local log_file="${WARP_STATE_DIR}/wireproxy.log"
  wireproxy -c "${WARP_STATE_DIR}/wireproxy.conf" >"${log_file}" 2>&1 </dev/null &
  local wp_pid=$!

  local deadline=$(( $(date +%s) + WARP_STARTUP_TIMEOUT ))
  while [ "$(date +%s)" -le "${deadline}" ]; do
    if ! kill -0 "${wp_pid}" >/dev/null 2>&1; then
      reason="$(tail_sanitized_log "${log_file}")"
      echo "[WARP] wireproxy exited early (${reason:-no stderr}); booting with direct outbound."
      return 0
    fi
    if probe_proxy_port "${WARP_PROXY_PORT}"; then
      echo "[WARP] wireproxy SOCKS5 ready at 127.0.0.1:${WARP_PROXY_PORT} (pid=${wp_pid})."
      return 0
    fi
    sleep 0.5
  done

  echo "[WARP] 127.0.0.1:${WARP_PROXY_PORT} still warming up after ${WARP_STARTUP_TIMEOUT}s; continuing boot (Python will auto-detect or fall back to direct)."
  return 0
}

( start_warp_proxy ) || true

exec "$@"
