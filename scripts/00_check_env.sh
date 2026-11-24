#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

check_macos_host() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[ERROR] This toolchain requires macOS (uses hdiutil, asr, codesign, clang)." >&2
    exit 1
  fi
}

check_cli_tools() {
  local missing=()
  for tool in curl tar unzip python3 clang codesign hdiutil asr; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if (( ${#missing[@]} > 0 )); then
    echo "[ERROR] Missing core macOS tools: ${missing[*]}" >&2
    echo "        Run: xcode-select --install" >&2
    exit 1
  fi

  local brew_missing=()
  for tool in zstd iproxy irecovery sshpass; do
    if ! command -v "$tool" >/dev/null 2>&1 && [[ ! -x "/opt/homebrew/bin/$tool" ]] && [[ ! -x "/usr/local/bin/$tool" ]]; then
      brew_missing+=("$tool")
    fi
  done
  if (( ${#brew_missing[@]} > 0 )); then
    echo "[!] Missing Homebrew tools: ${brew_missing[*]}" >&2
    echo "    Install them with:" >&2
    echo "      brew install zstd libimobiledevice libirecovery libusb" >&2
    echo "      brew install hudochenkov/sshpass/sshpass" >&2
    exit 1
  fi

  if [[ ! -f "/opt/homebrew/lib/libusb-1.0.dylib" && ! -f "/usr/local/lib/libusb-1.0.dylib" ]]; then
    echo "[!] libusb-1.0.dylib not found. Run: brew install libusb" >&2
    exit 1
  fi
}

ensure_python_venv() {
  if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
    echo "[*] Creating Python virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
  fi
  if ! "$VENV_DIR/bin/python3" -c "import pyimg4, capstone, remotezip" >/dev/null 2>&1; then
    echo "[*] Installing required Python packages (pyimg4, capstone, remotezip)..."
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet pyimg4 capstone remotezip
  fi
}

check_disk_space() {
  local min_gb="${1:-30}"
  local avail_kb
  avail_kb="$(df -k "$ROOT_DIR" | awk 'NR==2 {print $4}')"
  local avail_gb=$(( avail_kb / 1024 / 1024 ))
  if (( avail_gb < min_gb )); then
    echo "[WARN] Free disk space at $ROOT_DIR is ${avail_gb} GB (recommended >= ${min_gb} GB for 3 IPSWs + hybrid RootFS)." >&2
  fi
}

check_device_cpid() {
  local force="${1:-0}"
  local irec
  irec="$(command -v irecovery || echo /opt/homebrew/bin/irecovery)"
  local info=""
  local attempt
  for attempt in 1 2 3 4 5; do
    if info="$("$irec" -q 2>&1)" && printf '%s\n' "$info" | grep -q '^CPID:'; then
      break
    fi
    info=""
    sleep 0.4
  done
  if [[ -z "$info" ]]; then
    echo "[!] USB DFU/Recovery handle was not ready yet (transient USB drop)." >&2
    return 1
  fi

  local cpid mode pwnd ecid
  cpid="$(printf '%s\n' "$info" | awk -F': ' '/^CPID:/ {print tolower($2)}' | tr -d '\r')"
  mode="$(printf '%s\n' "$info" | awk -F': ' '/^MODE:/ {print $2}' | tr -d '\r')"
  pwnd="$(printf '%s\n' "$info" | awk -F': ' '/^PWND:/ {print $2}' | tr -d '\r')"
  ecid="$(printf '%s\n' "$info" | awk -F': ' '/^ECID:/ {print $2}' | tr -d '\r')"

  echo "[*] Detected device: MODE=${mode:-unknown} CPID=${cpid:-unknown} PWND=${pwnd:-none}"

  if [[ "$cpid" == "0x8003" ]]; then
    echo "[+] Confirmed TSMC A9 (iPhone 6s N71mAP / s8003)."
    return 0
  elif [[ "$cpid" == "0x8000" ]]; then
    if [[ "$force" == "1" ]]; then
      echo "[WARN] Samsung A9 (CPID 0x8000 / N71AP) detected — proceeding because --force was passed." >&2
      echo "       Note: Kernel/DeviceTree patches were verified on TSMC s8003 (N71mAP); Samsung N71AP is experimental." >&2
      return 0
    else
      echo "[STOP] Samsung A9 (CPID 0x8000 / N71AP) detected!" >&2
      echo "       This port is verified on TSMC A9 (CPID 0x8003 / N71mAP) by default." >&2
      echo "       If you explicitly want to experiment on Samsung N71AP, re-run with --force." >&2
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-CPID_CHECK}" "00_check_env.sh" "$LINENO" 2 "cpid_0x8000_blocked" "Samsung A9 CPID 0x8000 blocked"
        telemetry_mark_reported
      fi
      return 2
    fi
  else
    if [[ "$force" == "1" ]]; then
      echo "[WARN] Unexpected CPID (${cpid:-unknown}) — continuing only because --force was passed." >&2
      return 0
    fi
    echo "[STOP] Unsupported device CPID: ${cpid:-unknown} (expected iPhone 6s TSMC 0x8003)." >&2
    if declare -F telemetry_send >/dev/null 2>&1; then
      telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-CPID_CHECK}" "00_check_env.sh" "$LINENO" 2 "cpid_unsupported_blocked" "Unsupported CPID ${cpid:-unknown}"
      telemetry_mark_reported
    fi
    return 2
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-env}" in
    env)
      check_macos_host
      check_cli_tools
      ensure_python_venv
      check_disk_space 30
      echo "[+] Host environment verified."
      ;;
    cpid)
      check_device_cpid "${2:-0}"
      ;;
    *)
      echo "Usage: $0 [env | cpid <0|1>]" >&2
      exit 2
      ;;
  esac
fi
