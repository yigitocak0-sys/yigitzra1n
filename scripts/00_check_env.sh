#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$ROOT_DIR/.venv"

for _p in /opt/homebrew/bin /usr/local/bin; do
  if [[ -d "$_p" && ":$PATH:" != *":$_p:"* ]]; then
    export PATH="$_p:$PATH"
  fi
done
unset _p

find_irecovery_bin() {
  local cand
  cand="$(command -v irecovery 2>/dev/null || true)"
  if [[ -n "$cand" && -x "$cand" ]]; then
    printf '%s' "$cand"
    return 0
  fi
  for cand in /opt/homebrew/bin/irecovery /usr/local/bin/irecovery; do
    if [[ -x "$cand" ]]; then
      printf '%s' "$cand"
      return 0
    fi
  done
  printf '%s' "/opt/homebrew/bin/irecovery"
}

find_brew_bin() {
  local cand
  cand="$(command -v brew 2>/dev/null || true)"
  if [[ -n "$cand" && -x "$cand" ]]; then
    printf '%s' "$cand"
    return 0
  fi
  for cand in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [[ -x "$cand" ]]; then
      printf '%s' "$cand"
      return 0
    fi
  done
  return 1
}

check_macos_host() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "[ERROR] This toolchain requires macOS (uses hdiutil, asr, codesign, clang)." >&2
    if declare -F telemetry_send >/dev/null 2>&1; then
      telemetry_send "install_fail" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-ENV_CHECK}" "00_check_env.sh" "$LINENO" 1 "non_macos_host" "OS=$(uname -s)"
      telemetry_mark_reported
    fi
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
    if declare -F telemetry_send >/dev/null 2>&1; then
      telemetry_send "install_fail" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-ENV_CHECK}" "00_check_env.sh" "$LINENO" 1 "missing_xcode_cli" "Missing macOS tools: ${missing[*]}"
      telemetry_mark_reported
    fi
    exit 1
  fi

  local brew_missing=()
  local brew_pkgs=()
  local need_sshpass=0
  local need_libusb=0

  command -v zstd >/dev/null 2>&1 || [[ -x "/opt/homebrew/bin/zstd" ]] || [[ -x "/usr/local/bin/zstd" ]] || { brew_missing+=("zstd"); brew_pkgs+=("zstd"); }
  command -v iproxy >/dev/null 2>&1 || [[ -x "/opt/homebrew/bin/iproxy" ]] || [[ -x "/usr/local/bin/iproxy" ]] || { brew_missing+=("iproxy"); brew_pkgs+=("libimobiledevice"); }
  command -v irecovery >/dev/null 2>&1 || [[ -x "/opt/homebrew/bin/irecovery" ]] || [[ -x "/usr/local/bin/irecovery" ]] || { brew_missing+=("irecovery"); brew_pkgs+=("libirecovery"); }
  command -v sshpass >/dev/null 2>&1 || [[ -x "/opt/homebrew/bin/sshpass" ]] || [[ -x "/usr/local/bin/sshpass" ]] || { brew_missing+=("sshpass"); need_sshpass=1; }

  if [[ ! -f "/opt/homebrew/lib/libusb-1.0.dylib" && ! -f "/usr/local/lib/libusb-1.0.dylib" ]]; then
    need_libusb=1
    brew_missing+=("libusb")
    brew_pkgs+=("libusb")
  fi

  if (( ${#brew_missing[@]} > 0 )); then
    local brew_bin=""
    brew_bin="$(find_brew_bin || true)"

    if [[ -z "$brew_bin" ]]; then
      echo "[!] Missing tools: ${brew_missing[*]}" >&2
      echo "[!] Homebrew is not installed on this Mac." >&2
      echo "    First, install Homebrew:" >&2
      echo '      /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' >&2
      echo "    Then install required tools (or re-run yigitzra1n and it will install them automatically):" >&2
      echo "      brew install zstd libimobiledevice libirecovery libusb" >&2
      echo "      brew install hudochenkov/sshpass/sshpass" >&2
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "install_fail" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-ENV_CHECK}" "00_check_env.sh" "$LINENO" 1 "missing_homebrew" "Missing Homebrew and tools: ${brew_missing[*]}"
        telemetry_mark_reported
      fi
      exit 1
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
      echo "[!] Missing Homebrew tools: ${brew_missing[*]}" >&2
      echo "    Run as your normal user (not sudo):" >&2
      echo "      brew install ${brew_pkgs[*]}" >&2
      if (( need_sshpass == 1 )); then
        echo "      brew install hudochenkov/sshpass/sshpass" >&2
      fi
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "install_fail" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-ENV_CHECK}" "00_check_env.sh" "$LINENO" 1 "missing_brew_tools" "Missing Homebrew tools under root: ${brew_missing[*]}"
        telemetry_mark_reported
      fi
      exit 1
    fi

    echo "[*] Missing Homebrew tools: ${brew_missing[*]}"
    echo "[*] Homebrew found ($brew_bin) — installing missing dependencies automatically..."
    local install_rc=0
    if (( ${#brew_pkgs[@]} > 0 )); then
      "$brew_bin" install "${brew_pkgs[@]}" || install_rc=$?
    fi
    if (( install_rc == 0 && need_sshpass == 1 )); then
      "$brew_bin" install hudochenkov/sshpass/sshpass || install_rc=$?
    fi

    for _p in /opt/homebrew/bin /usr/local/bin; do
      if [[ -d "$_p" && ":$PATH:" != *":$_p:"* ]]; then
        export PATH="$_p:$PATH"
      fi
    done
    unset _p

    local still_missing=()
    local t
    for t in zstd iproxy irecovery sshpass; do
      if ! command -v "$t" >/dev/null 2>&1 && [[ ! -x "/opt/homebrew/bin/$t" ]] && [[ ! -x "/usr/local/bin/$t" ]]; then
        still_missing+=("$t")
      fi
    done
    if [[ ! -f "/opt/homebrew/lib/libusb-1.0.dylib" && ! -f "/usr/local/lib/libusb-1.0.dylib" ]]; then
      still_missing+=("libusb")
    fi

    if (( install_rc != 0 || ${#still_missing[@]} > 0 )); then
      echo "[!] Automatic Homebrew installation failed or tools are still missing: ${still_missing[*]:-${brew_missing[*]}}" >&2
      echo "    Install them manually with:" >&2
      echo "      brew install zstd libimobiledevice libirecovery libusb" >&2
      echo "      brew install hudochenkov/sshpass/sshpass" >&2
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "install_fail" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-ENV_CHECK}" "00_check_env.sh" "$LINENO" 1 "missing_brew_tools" "Brew auto-install failed: ${still_missing[*]:-${brew_missing[*]}}"
        telemetry_mark_reported
      fi
      exit 1
    fi

    echo "[+] Homebrew dependencies installed!"
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
  irec="$(find_irecovery_bin)"
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

  local cpid bdid mode pwnd ecid
  cpid="$(printf '%s\n' "$info" | awk -F': ' '/^CPID:/ {print tolower($2)}' | tr -d '\r')"
  bdid="$(printf '%s\n' "$info" | awk -F': ' '/^BDID:/ {print tolower($2)}' | tr -d '\r')"
  mode="$(printf '%s\n' "$info" | awk -F': ' '/^MODE:/ {print $2}' | tr -d '\r')"
  pwnd="$(printf '%s\n' "$info" | awk -F': ' '/^PWND:/ {print $2}' | tr -d '\r')"
  ecid="$(printf '%s\n' "$info" | awk -F': ' '/^ECID:/ {print $2}' | tr -d '\r')"

  echo "[*] Detected device: MODE=${mode:-unknown} CPID=${cpid:-unknown} BDID=${bdid:-unknown} PWND=${pwnd:-none}"

  if [[ "$cpid" == "0x8003" || "$cpid" == "0x8000" ]]; then
    if [[ -n "$bdid" && "$bdid" != "0x04" ]]; then
      case "$bdid" in
        0x06)
          echo "[STOP] iPhone 6s Plus (CPID $cpid / BDID 0x06 / N66AP-N66mAP) detected — NOT supported yet!" >&2
          echo "       This release only supports the 4.7-inch iPhone 6s (BDID 0x04 / N71AP-N71mAP)." >&2
          echo "       Restoring N71 images onto a 6s Plus fails BuildManifest/panel/touch compatibility." >&2
          if declare -F telemetry_send >/dev/null 2>&1; then
            telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-BDID_CHECK}" "00_check_env.sh" "$LINENO" 2 "bdid_0x06_6s_plus_blocked" "iPhone 6s Plus (BDID 0x06 / $cpid) blocked"
            telemetry_mark_reported
          fi
          return 2
          ;;
        0x02)
          echo "[STOP] iPhone SE (1st gen / CPID $cpid / BDID 0x02 / N69AP-N69uAP) detected — NOT supported!" >&2
          echo "       This release only supports the 4.7-inch iPhone 6s (BDID 0x04)." >&2
          if declare -F telemetry_send >/dev/null 2>&1; then
            telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-BDID_CHECK}" "00_check_env.sh" "$LINENO" 2 "bdid_0x02_se1_blocked" "iPhone SE 1st gen (BDID 0x02 / $cpid) blocked"
            telemetry_mark_reported
          fi
          return 2
          ;;
        0x10|0x12)
          echo "[STOP] iPad 5th gen (CPID $cpid / BDID $bdid) detected — NOT supported!" >&2
          echo "       This release targets the iPhone 6s (BDID 0x04), not the donor iPad." >&2
          if declare -F telemetry_send >/dev/null 2>&1; then
            telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-BDID_CHECK}" "00_check_env.sh" "$LINENO" 2 "bdid_ipad5_blocked" "iPad 5th gen (BDID $bdid / $cpid) blocked"
            telemetry_mark_reported
          fi
          return 2
          ;;
        *)
          echo "[STOP] Unsupported A9 board BDID: $bdid (CPID $cpid). Expected iPhone 6s (BDID 0x04)." >&2
          if declare -F telemetry_send >/dev/null 2>&1; then
            telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-BDID_CHECK}" "00_check_env.sh" "$LINENO" 2 "bdid_unsupported_blocked" "Unsupported A9 BDID $bdid ($cpid)"
            telemetry_mark_reported
          fi
          return 2
          ;;
      esac
    fi
  fi

  if [[ "$cpid" == "0x8003" ]]; then
    export YIGITZRA1N_DEVICE_SOC="tsmc_0x8003"
    echo "[+] Confirmed TSMC A9 (iPhone 6s N71mAP / s8003, BDID ${bdid:-0x04})."
    return 0
  elif [[ "$cpid" == "0x8000" ]]; then
    if [[ "$force" == "1" || "${YIGITZRA1N_S8000_CONFIRMED:-0}" == "1" ]]; then
      export YIGITZRA1N_DEVICE_SOC="samsung_0x8000_forced"
      if [[ "${YIGITZRA1N_S8000_CONFIRMED:-0}" != "1" ]]; then
        echo "[WARN] Samsung A9 (CPID 0x8000 / N71AP, BDID ${bdid:-0x04}) detected — proceeding because --force was passed." >&2
        echo "       Note: Kernel/DeviceTree patches were verified on TSMC s8003 (N71mAP); Samsung N71AP is experimental." >&2
      fi
      return 0
    elif [[ -t 0 ]]; then
      echo "" >&2
      echo "======================================================================" >&2
      echo "  NOTE!!! Your phone is using an A9 chip from Samsung (CPID 0x8000 / N71AP)." >&2
      echo "  Samsung A9 support is EXPERIMENTAL (primary verified target: TSMC s8003)." >&2
      echo "======================================================================" >&2
      printf "  Are you sure you want to continue? [y/N]: " >&2
      local s8000_ans=""
      IFS= read -r s8000_ans || true
      if [[ "${s8000_ans:-N}" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        export YIGITZRA1N_DEVICE_SOC="samsung_0x8000_forced"
        export YIGITZRA1N_S8000_CONFIRMED=1
        echo "[+] Continuing on experimental Samsung A9 (CPID 0x8000 / N71AP)." >&2
        return 0
      fi
      echo "[*] Aborted by user (Samsung A9 confirmation declined)." >&2
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-CPID_CHECK}" "00_check_env.sh" "$LINENO" 2 "cpid_0x8000_declined" "User declined experimental Samsung A9 prompt"
        telemetry_mark_reported
      fi
      return 2
    else
      echo "[STOP] Samsung A9 (CPID 0x8000 / N71AP) detected (non-interactive shell)." >&2
      echo "       Re-run interactively to confirm, or pass --force." >&2
      if declare -F telemetry_send >/dev/null 2>&1; then
        telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-CPID_CHECK}" "00_check_env.sh" "$LINENO" 2 "cpid_0x8000_blocked" "Samsung A9 CPID 0x8000 blocked (non-interactive)"
        telemetry_mark_reported
      fi
      return 2
    fi
  else
    if [[ "$force" == "1" ]]; then
      export YIGITZRA1N_DEVICE_SOC="unexpected_forced"
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

check_connected_device_early() {
  local force="${1:-0}"
  local irec
  irec="$(find_irecovery_bin)"

  if [[ -x "$irec" ]]; then
    local qout=""
    qout="$(python3 - "$irec" <<'PY' 2>/dev/null || true
import subprocess, sys
try:
    r = subprocess.run([sys.argv[1], "-q"], capture_output=True, text=True, timeout=1.5)
    if "CPID:" in r.stdout:
        print(r.stdout.strip())
except Exception:
    pass
PY
)"
    if [[ -n "$qout" ]]; then
      local cpid_rc=0
      check_device_cpid "$force" || cpid_rc=$?
      if [[ "$cpid_rc" -eq 2 ]]; then
        exit 1
      fi
      return 0
    fi
  fi

  command -v idevice_id >/dev/null 2>&1 || return 0
  command -v ideviceinfo >/dev/null 2>&1 || return 0

  local udid=""
  udid="$(idevice_id -l 2>/dev/null | grep -v -E 'SSHRD|^ffffffff' | head -n 1 | tr -d '\r' || true)"
  [[ -n "$udid" ]] || return 0

  local dev_meta=""
  dev_meta="$(python3 - "$udid" <<'PY' 2>/dev/null || true
import subprocess, sys
def prop(key):
    r = subprocess.run(["ideviceinfo", "-u", sys.argv[1], "-k", key], capture_output=True, text=True, timeout=2.0)
    return r.stdout.strip() if r.returncode == 0 else ""
pt = prop("ProductType")
hw = prop("HardwareModel")
if pt or hw:
    print(f"{pt}|{hw}")
PY
)"
  [[ -n "$dev_meta" ]] || return 0

  local ptype="${dev_meta%%|*}"
  local hw="${dev_meta#*|}"
  [[ -n "$ptype" ]] || return 0

  echo "[*] Connected USB iOS device detected: ProductType=$ptype HardwareModel=${hw:-unknown}"

  if [[ "$ptype" == "iPhone8,2" ]]; then
    echo "[STOP] iPhone 6s Plus ($ptype / ${hw:-N66*}) detected — NOT supported yet!" >&2
    echo "       This release only supports the 4.7-inch iPhone 6s (iPhone8,1 / N71mAP-N71AP)." >&2
    echo "       Stopping before downloading ~30 GB of IPSWs." >&2
    if declare -F telemetry_send >/dev/null 2>&1; then
      telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-PREPARE_1_ENV}" "00_check_env.sh" "$LINENO" 2 "product_6s_plus_blocked" "iPhone 6s Plus ($ptype / $hw) blocked in prepare"
      telemetry_mark_reported
    fi
    exit 1
  elif [[ "$ptype" != "iPhone8,1" ]]; then
    echo "[STOP] Connected device ($ptype / ${hw:-unknown}) is not an iPhone 6s (iPhone8,1)." >&2
    echo "       Stopping before downloading ~30 GB of IPSWs." >&2
    if declare -F telemetry_send >/dev/null 2>&1; then
      telemetry_send "security_block" "${TELEMETRY_CMD:-${CMD:-unknown}}" "${TELEMETRY_STEP:-PREPARE_1_ENV}" "00_check_env.sh" "$LINENO" 2 "product_unsupported_blocked" "Unsupported $ptype ($hw) blocked in prepare"
      telemetry_mark_reported
    fi
    exit 1
  elif [[ ( "$hw" == "N71AP" || "$hw" == "n71ap" ) && "$force" != "1" ]]; then
    echo "[WARN] NOTE!!! Connected iPhone 6s uses a Samsung A9 chip (N71AP / CPID 0x8000)." >&2
    echo "       Samsung A9 support is EXPERIMENTAL. Preparation will continue; you will be asked to confirm before install/boot (or pass --force)." >&2
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
