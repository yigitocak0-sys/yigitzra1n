#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CALLER_PWD="${YIGITZRA1N_CALLER_PWD:-$PWD}"
ORIG_ARGS=("$@")
APP_HOME="${YIGITZRA1N_HOME:-$HOME/Library/Application Support/yigitzra1n}"
BIN_DIR="${YIGITZRA1N_BIN_DIR:-$HOME/.local/bin}"
GLOBAL_BIN="$BIN_DIR/yigitzra1n"

_resolve_real_dir() {
  local d="$1"
  if [[ -d "$d" ]]; then
    (cd "$d" && pwd -P)
  else
    printf '%s' "$d"
  fi
}

_append_path_profile() {
  local rc_file="$1"
  local bin_dir="$2"
  local marker="# >>> yigitzra1n PATH >>>"
  if [[ -f "$rc_file" ]] && grep -qF "$marker" "$rc_file" 2>/dev/null; then
    return 1
  fi
  if [[ -f "$rc_file" ]] && grep -qF "$bin_dir" "$rc_file" 2>/dev/null; then
    return 1
  fi
  mkdir -p "$(dirname "$rc_file")"
  {
    printf '\n%s\n' "$marker"
    printf 'export PATH="%s:$PATH"\n' "$bin_dir"
    printf '# <<< yigitzra1n PATH <<<\n'
  } >> "$rc_file"
  return 0
}

ensure_global_launcher_and_path() {
  local app_dir="${1:-$APP_HOME}"
  local fallback_src="${2:-}"
  if [[ -z "$fallback_src" && -f "$app_dir/.yigitzra1n_source_dir" ]]; then
    fallback_src="$(head -n 1 "$app_dir/.yigitzra1n_source_dir" 2>/dev/null || true)"
  fi
  if [[ -z "$fallback_src" ]]; then
    fallback_src="$app_dir"
  fi

  mkdir -p "$BIN_DIR"
  local escaped_fallback=""
  escaped_fallback="$(printf '%q' "$fallback_src")"

  local tmp_launcher=""
  tmp_launcher="$(mktemp "$BIN_DIR/.yigitzra1n.XXXXXX")"
  cat > "$tmp_launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
APP_HOME="\${YIGITZRA1N_HOME:-\$HOME/Library/Application Support/yigitzra1n}"
FALLBACK_SRC=$escaped_fallback
if [[ ! -f "\$APP_HOME/yigitzra1n.sh" ]]; then
  echo "[*] Setting up yigitzra1n in \$APP_HOME..."
  mkdir -p "\$APP_HOME"
  if [[ -n "\$FALLBACK_SRC" && -f "\$FALLBACK_SRC/yigitzra1n.sh" ]]; then
    cp -ca "\$FALLBACK_SRC/." "\$APP_HOME/" 2>/dev/null || cp -a "\$FALLBACK_SRC/." "\$APP_HOME/"
    printf '%s\n' "\$FALLBACK_SRC" > "\$APP_HOME/.yigitzra1n_source_dir"
    date +%s > "\$APP_HOME/.yigitzra1n_installed"
  elif command -v curl >/dev/null 2>&1 && command -v tar >/dev/null 2>&1; then
    _tmp_dl="\$(mktemp -d /tmp/yigitz_dl.XXXXXX)"
    trap 'rm -rf "\$_tmp_dl"' EXIT
    _dl_url="\${YIGITZRA1N_UPDATE_TARBALL_URL:-https://github.com/yigitocak0-sys/yigitzra1n/archive/refs/heads/main.tar.gz}"
    curl -fsSL --connect-timeout 6 --max-time 30 \
      -o "\$_tmp_dl/repo.tar.gz" \
      "\$_dl_url"
    tar -xzf "\$_tmp_dl/repo.tar.gz" -C "\$_tmp_dl"
    _extracted="\$(find "\$_tmp_dl" -maxdepth 1 -type d ! -path "\$_tmp_dl" | head -n 1)"
    [[ -n "\$_extracted" && -f "\$_extracted/yigitzra1n.sh" ]] || {
      echo "[ERROR] Failed to download yigitzra1n." >&2
      exit 1
    }
    cp -a "\$_extracted/." "\$APP_HOME/"
    date +%s > "\$APP_HOME/.yigitzra1n_installed"
    rm -rf "\$_tmp_dl"
    trap - EXIT
  else
    echo "[ERROR] \$APP_HOME is missing and curl/tar unavailable to restore it." >&2
    exit 1
  fi
  chmod +x "\$APP_HOME/yigitzra1n.sh"
fi
exec "\$APP_HOME/yigitzra1n.sh" "\$@"
EOF
  chmod 755 "$tmp_launcher"
  if [[ -f "$GLOBAL_BIN" ]] && cmp -s "$tmp_launcher" "$GLOBAL_BIN" 2>/dev/null; then
    rm -f "$tmp_launcher"
  else
    mv -f "$tmp_launcher" "$GLOBAL_BIN"
  fi

  local sys_linked=0
  if [[ "${YIGITZRA1N_NO_SYSTEM_BIN:-0}" != "1" ]]; then
    local sdir=""
    for sdir in /opt/homebrew/bin /usr/local/bin; do
      if [[ -d "$sdir" && -w "$sdir" ]]; then
        if [[ ! -e "$sdir/yigitzra1n" || -L "$sdir/yigitzra1n" ]]; then
          ln -sf "$GLOBAL_BIN" "$sdir/yigitzra1n" 2>/dev/null && sys_linked=1 || true
        fi
      fi
    done
  fi

  local rc_updated=0
  local rc=""
  _append_path_profile "$HOME/.zshrc" "$BIN_DIR" && rc_updated=1 || true
  for rc in "$HOME/.bash_profile" "$HOME/.bashrc"; do
    if [[ -f "$rc" ]]; then
      _append_path_profile "$rc" "$BIN_DIR" && rc_updated=1 || true
    fi
  done

  if [[ "$rc_updated" -eq 1 && "${YIGITZRA1N_QUIET_INSTALL:-0}" != "1" ]]; then
    if [[ "$sys_linked" -eq 1 ]] || command -v yigitzra1n >/dev/null 2>&1; then
      echo "[+] Installed global 'yigitzra1n' command — you can now run 'yigitzra1n' from anywhere."
    else
      echo "[+] Installed global 'yigitzra1n' command ($GLOBAL_BIN) and added $BIN_DIR to PATH (~/.zshrc)."
      echo "    Open a new terminal or run: source ~/.zshrc"
    fi
  fi
}

_version_gt() {
  local v1="$1" v2="$2"
  [[ -n "$v1" && -n "$v2" && "$v1" != "$v2" ]] || return 1
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$v1" "$v2" <<'PY' 2>/dev/null
import re, sys
def norm(s):
    return [int(x) for x in re.findall(r"\d+", s)]
sys.exit(0 if norm(sys.argv[1]) > norm(sys.argv[2]) else 1)
PY
    return $?
  fi
  [[ "$(printf '%s\n%s\n' "$v1" "$v2" | sort -V | tail -n 1)" == "$v1" ]]
}

_sync_missing_data_dirs() {
  local src="$1" dst="$2"
  local item=""
  for item in build ipsw .cache .venv .yigitzra1n_init; do
    [[ -e "$src/$item" ]] || continue
    if [[ ! -e "$dst/$item" ]]; then
      cp -ca "$src/$item" "$dst/$item" 2>/dev/null || cp -a "$src/$item" "$dst/$item"
    elif [[ -d "$src/$item" && -d "$dst/$item" ]]; then
      if command -v rsync >/dev/null 2>&1; then
        rsync -a --ignore-existing "$src/$item/" "$dst/$item/" 2>/dev/null || true
      elif command -v python3 >/dev/null 2>&1; then
        python3 - "$src/$item" "$dst/$item" <<'PY' 2>/dev/null || true
import os, pathlib, shutil, sys
sroot, droot = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
for sp in sroot.rglob("*"):
    rel = sp.relative_to(sroot)
    dp = droot / rel
    if dp.exists() or dp.is_symlink():
        continue
    dp.parent.mkdir(parents=True, exist_ok=True)
    if sp.is_symlink():
        os.symlink(os.readlink(sp), dp)
    elif sp.is_dir():
        dp.mkdir(parents=True, exist_ok=True)
    elif sp.is_file():
        shutil.copy2(sp, dp)
PY
      fi
    fi
  done
}

ensure_app_home_and_relocate() {
  local root_real="" app_real=""
  root_real="$(_resolve_real_dir "$ROOT_DIR")"
  mkdir -p "$(dirname "$APP_HOME")"
  app_real="$(_resolve_real_dir "$APP_HOME")"

  if [[ "${YIGITZRA1N_NO_RELOCATE:-0}" != "1" && "$root_real" != "$app_real" ]]; then
    if [[ ! -f "$APP_HOME/.yigitzra1n_installed" || ! -f "$APP_HOME/yigitzra1n.sh" ]]; then
      echo "[*] Installing yigitzra1n into $APP_HOME..."
      mkdir -p "$APP_HOME"
      cp -ca "$ROOT_DIR/." "$APP_HOME/" 2>/dev/null || cp -a "$ROOT_DIR/." "$APP_HOME/"
      chmod +x "$APP_HOME/yigitzra1n.sh"
      printf '%s\n' "$root_real" > "$APP_HOME/.yigitzra1n_source_dir"
      date +%s > "$APP_HOME/.yigitzra1n_installed"
    else
      _sync_missing_data_dirs "$ROOT_DIR" "$APP_HOME"
      local src_ver="" app_ver=""
      [[ -f "$ROOT_DIR/VERSION" ]] && src_ver="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION" 2>/dev/null || true)"
      [[ -f "$APP_HOME/VERSION" ]] && app_ver="$(tr -d '[:space:]' < "$APP_HOME/VERSION" 2>/dev/null || true)"
      if _version_gt "$src_ver" "$app_ver"; then
        local citem=""
        for citem in yigitzra1n.sh VERSION LICENSE README.md tutorial.txt config entitlements scripts src .git; do
          [[ -e "$ROOT_DIR/$citem" ]] || continue
          rm -rf "$APP_HOME/$citem"
          cp -ca "$ROOT_DIR/$citem" "$APP_HOME/$citem" 2>/dev/null || cp -a "$ROOT_DIR/$citem" "$APP_HOME/$citem"
        done
        chmod +x "$APP_HOME/yigitzra1n.sh"
      fi
      printf '%s\n' "$root_real" > "$APP_HOME/.yigitzra1n_source_dir"
    fi
    ensure_global_launcher_and_path "$APP_HOME" "$root_real"
    exec env YIGITZRA1N_FALLBACK_SRC="$root_real" YIGITZRA1N_CALLER_PWD="$CALLER_PWD" "$APP_HOME/yigitzra1n.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
  fi

  if [[ "$root_real" == "$app_real" && ! -f "$APP_HOME/.yigitzra1n_installed" ]]; then
    date +%s > "$APP_HOME/.yigitzra1n_installed"
  fi
  ensure_global_launcher_and_path "$ROOT_DIR" "${YIGITZRA1N_FALLBACK_SRC:-}"
}

ensure_app_home_and_relocate

source "$ROOT_DIR/config/firmware_urls.env"
source "$ROOT_DIR/scripts/telemetry.sh"
source "$ROOT_DIR/scripts/00_check_env.sh"

IPSW_DIR="${IPSW_DIR:-$ROOT_DIR/ipsw}"
BUILD_DIR="$ROOT_DIR/build"
ENGINE_DIR="$BUILD_DIR/engine"
LOCAL_API_DIR="$BUILD_DIR/local_api"
PROCURSUS_DIR="$ROOT_DIR/.cache/procursus"
VENV_PY="$ROOT_DIR/.venv/bin/python3"

mkdir -p "$BUILD_DIR/.tmp"
export TMPDIR="$BUILD_DIR/.tmp"

FORCE=0
SKIP_DOWNLOAD=0
QUIET=0
NO_UPDATE=0
SKIP_SILEO=0
BLOCK_PATH=""
BOOT_ARGS=""
DEVICE_HOST="127.0.0.1"
DEVICE_PORT="2222"
PORT_EXPLICIT=0
IC_INFO=""
OUT_PATH=""
ASSUME_YES=0
LIST_MODE=0
UNINSTALL_TARGET=""
POS_ARG=""
MESSAGE_PARTS=()

usage() {
  cat <<'EOF'
yigitzra1n — iOS 16.7.16 (20H392) for iPhone 6s (N71mAP / TSMC s8003, experimental Samsung s8000)

Prerequisite: Your iPhone 6s must be running iOS 15.8.8 (19H422) before installation.

Commands:
  doctor        Check macOS tools, Brew packages, Python venv, IPSWs, boot block & connected device.
  prepare       Download (or reuse from ipsw/, symlinks supported) the 3 stock Apple IPSWs
                and build all patched images (kernel v6, DeviceTree v5, RamDisks, Hybrid RootFS).
  install       Interactive guided restore & permanent boot-block setup (prompts for DFU mode).
  boot          Tethered boot an installed iPhone 6s into iOS 16.7.16 (verbose logs + centered
                yigitzra1n logo by default; auto-installs Sileo & default repos; --quiet hides text).
  sileo         Install/update Sileo & default repos over USB SSH (no reboot needed).
  sideload      Native IPA sideloader over USB SSH (`sideload <app.ipa> | --list | --uninstall <id>`).
  panic-check   Pull all panic-full*.ips logs from the connected iOS 16 device to your Mac.
  panic-send    Pull, zip, and send device panic-full logs to the developer after confirmation.
  export-block  Export your saved SHC/PTE boot block to a backup path (`export-block [path]`).
  import-block  Import a backed-up SHC/PTE boot block (`import-block <file.bin>`).
  ssh-ramdisk   Boot the maintenance OpenSSH RamDisk (port 22 over USB via iproxy).
  uninstall     Reset the unencrypted iOS 16 APFS container (/dev/disk0s1) via SSH RamDisk so
                stock iOS 15.8.8 restores cleanly without iTunes/Finder Error 9.
  activate      Activate device & complete initial setup over SSH/USB.
  patch-device  Apply/refresh live userland patches over SSH (optional --ic-info).
  message       Send a direct bug report, result, or thank-you message to Yigit (I read them!).
  messages      View your sent messages and check for developer replies.
  version       Show installed version and latest GitHub version without updating.
  update        Check GitHub and update yigitzra1n in place (supports both git and ZIP installs).
  clean         Remove build/ artifacts on your Mac (keeps ipsw/ and .cache/; does not touch device).

Options:
  --ipsw-dir DIR     Custom directory containing the 3 stock Apple IPSWs (default: ./ipsw)
  --skip-download    Fail instead of downloading missing IPSWs/assets
  --force            Override unsupported CPID safety guard
  --no-update        Skip automatic GitHub update check on startup
  --quiet, -q        Disable verbose kernel boot console (-v)
  --verbose          Explicitly enable verbose kernel boot console (-v, default)
  --skip-sileo       Skip post-boot Sileo check/install over USB SSH
  --block PATH       Explicit path to device boot block (.bin)
  --boot-args STR    Custom kernel boot-args for `boot`
  --host IP          SSH host (default: 127.0.0.1)
  --port PORT        SSH port (default: auto USB iproxy)
  --ic-info PATH     Install your device's FairPlay IC-Info.sisv during `patch-device`
  --out PATH         Destination directory/file for `panic-check` or `export-block`
  --yes, -y          Confirm prompt non-interactively (e.g. `panic-send`)
  --list             List non-stock apps in /Applications (`sideload --list`)
  --uninstall NAME   Remove a sideloaded app by name or bundle ID (`sideload --uninstall ...`)
EOF
}

CMD="${1:-}"
[[ -n "$CMD" ]] || { usage; exit 1; }
case "$CMD" in
  -h|--help|help) usage; exit 0 ;;
esac
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipsw-dir) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; IPSW_DIR="$2"; shift 2 ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    --force) FORCE=1; shift ;;
    --no-update) NO_UPDATE=1; shift ;;
    --quiet|-q) QUIET=1; shift ;;
    --verbose) QUIET=0; shift ;;
    --skip-sileo) SKIP_SILEO=1; shift ;;
    --block) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; BLOCK_PATH="$2"; shift 2 ;;
    --boot-args) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; BOOT_ARGS="$2"; shift 2 ;;
    --host) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; DEVICE_HOST="$2"; shift 2 ;;
    --port) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; DEVICE_PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
    --ic-info) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; IC_INFO="$2"; shift 2 ;;
    --out) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; OUT_PATH="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --list) LIST_MODE=1; shift ;;
    --uninstall) [[ $# -ge 2 ]] || { echo "[ERROR] Missing argument for $1" >&2; exit 2; }; UNINSTALL_TARGET="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ "$CMD" == "message" && "$1" != -* ]]; then
        MESSAGE_PARTS+=("$1")
        shift
      elif [[ ( "$CMD" == "sideload" || "$CMD" == "export-block" || "$CMD" == "import-block" || "$CMD" == "panic-check" ) && "$1" != -* && -z "$POS_ARG" ]]; then
        POS_ARG="$1"
        shift
      else
        echo "[ERROR] Unknown option: $1" >&2
        usage
        exit 2
      fi
      ;;
  esac
done

resolve_update_python() {
  if [[ -x "$VENV_PY" ]]; then
    printf '%s' "$VENV_PY"
    return 0
  fi
  if xcode-select -p >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    printf '%s' "python3"
    return 0
  fi
  return 1
}

run_self_update() {
  local mode="${1:-auto}"
  local py_bin=""
  local updater="$ROOT_DIR/scripts/self_update.py"
  [[ -f "$updater" ]] || return 0
  py_bin="$(resolve_update_python 2>/dev/null || true)"
  if [[ -z "$py_bin" ]] && [[ "$mode" == "version" ]] && command -v python3 >/dev/null 2>&1; then
    py_bin="python3"
  fi
  [[ -n "$py_bin" ]] || {
    if [[ "$mode" == "apply" ]]; then
      echo "[ERROR] python3 / Xcode Command Line Tools required for update. Run: xcode-select --install" >&2
      return 1
    fi
    if [[ "$mode" == "version" ]]; then
      local local_ver="unknown"
      [[ -f "$ROOT_DIR/VERSION" ]] && local_ver="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION" 2>/dev/null || echo unknown)"
      echo "Installed version : $local_ver"
      echo "Install path      : $ROOT_DIR"
      echo "Latest version    : unavailable (python3 / Xcode Command Line Tools not installed)"
      return 0
    fi
    return 0
  }

  local args=(--root "$ROOT_DIR" --mode "$mode")
  [[ "$mode" == "apply" && "$FORCE" -eq 1 ]] && args+=(--force)

  local upd_rc=0
  "$py_bin" "$updater" "${args[@]}" || upd_rc=$?
  if [[ "$mode" == "apply" || "$mode" == "version" ]]; then
    if [[ "$upd_rc" -eq 10 || "$upd_rc" -eq 0 ]]; then
      return 0
    fi
    return "$upd_rc"
  fi

  if [[ "$upd_rc" -eq 10 ]]; then
    echo "[*] Restarting yigitzra1n with updated code..."
    exec env YIGITZRA1N_UPDATED=1 "$ROOT_DIR/yigitzra1n.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
  fi
  return 0
}

print_legacy_sileo_notice() {
  cat <<'NOTICE'
----------------------------------------------------------------------
  [NOTICE] Updating from an older yigitzra1n version?
    * Global CLI enabled: run 'yigitzra1n <command>' from anywhere (no cd needed!).
    * Already booted into iOS 16? Run:  yigitzra1n sileo
      (Installs Sileo & 5 default repos over USB — no reboot!)
    * Or run 'yigitzra1n boot' as usual — it installs them automatically on boot.
----------------------------------------------------------------------
NOTICE
}

if [[ "$CMD" == "version" ]]; then
  run_self_update "version"
  exit $?
fi

if [[ "$CMD" == "update" ]]; then
  run_self_update "apply"
  upd_exit=$?
  ensure_global_launcher_and_path "$ROOT_DIR" "${YIGITZRA1N_FALLBACK_SRC:-}"
  [[ "$upd_exit" -eq 0 ]] && print_legacy_sileo_notice
  exit "$upd_exit"
fi

if [[ "$CMD" != "clean" && "$CMD" != "version" && "$CMD" != "doctor" && "$CMD" != "export-block" && "$CMD" != "import-block" && "$NO_UPDATE" -eq 0 && "${YIGITZRA1N_NO_UPDATE:-0}" != "1" && "${YIGITZRA1N_UPDATED:-0}" != "1" ]]; then
  run_self_update "auto"
fi

if [[ "${YIGITZRA1N_UPDATED:-0}" == "1" && "$CMD" != "sileo" ]]; then
  print_legacy_sileo_notice
fi

export TELEMETRY_CMD="$CMD"
export TELEMETRY_STEP="INIT"
export TELEMETRY_PARENT=1
export TELEMETRY_MARKER_FILE="$BUILD_DIR/.tmp/.telemetry_reported"
rm -f "$TELEMETRY_MARKER_FILE" 2>/dev/null || true
telemetry_track_first_run "$ROOT_DIR" "$CMD"
if [[ "$CMD" != "clean" && "$CMD" != "messages" && "$CMD" != "update" && "${YIGITZRA1N_MSG_CHECKED:-0}" != "1" ]]; then
  export YIGITZRA1N_MSG_CHECKED=1
  telemetry_check_unread_messages || true
fi
trap 'exit 130' INT
trap 'telemetry_on_exit "$TELEMETRY_CMD" "$TELEMETRY_STEP" "$?"' EXIT
trap 'telemetry_trap_err "$TELEMETRY_CMD" "$TELEMETRY_STEP" "yigitzra1n.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR

find_ipsw() {
  local pat="$1"
  local f
  f="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$pat" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  [[ -n "$f" ]] || { echo "[ERROR] Missing IPSW matching $pat in $IPSW_DIR" >&2; exit 1; }
  echo "$f"
}

ensure_local_server() {
  YIGITZRA1N_IPSW_DIR="$IPSW_DIR" python3 - "$ROOT_DIR/scripts" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import pongo_wifi_boot
pongo_wifi_boot.ensure_local_server()
PY
}

run_engine_clean() {
  mkdir -p "$BUILD_DIR"
  python3 - "$ENGINE_DIR" "$BUILD_DIR/engine_session.log" "$@" <<'PY'
import errno, os, pty, re, select, subprocess, sys

cwd, log_path = sys.argv[1], sys.argv[2]
cmd = sys.argv[3:]
pat = re.compile(r"turdus(?:_m3rula|_merula|ra1n)?", re.IGNORECASE)

master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(cmd, cwd=cwd, stdin=0, stdout=slave_fd, stderr=slave_fd, close_fds=True)
os.close(slave_fd)

with open(log_path, "ab") as logf:
    while True:
        r, _, _ = select.select([master_fd], [], [], 0.25)
        if not r:
            if proc.poll() is not None:
                break
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError as e:
            if e.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        logf.write(chunk)
        logf.flush()
        txt = pat.sub("yigitzra1n", chunk.decode("utf-8", errors="replace"))
        sys.stdout.write(txt)
        sys.stdout.flush()

proc.wait()
os.close(master_fd)
sys.exit(proc.returncode)
PY
}

INSTALL_STATE_FILE="$BUILD_DIR/.install_state"

save_install_state() {
  mkdir -p "$BUILD_DIR"
  printf '%s\n' "$1" > "$INSTALL_STATE_FILE"
}

detect_install_state() {
  if [[ -f "$INSTALL_STATE_FILE" ]]; then
    tr -d ' \r\n' < "$INSTALL_STATE_FILE"
    return 0
  fi
  if ls "$ENGINE_DIR/image4"/*-signed-SEP.img4 >/dev/null 2>&1 && \
     (ls "$ENGINE_DIR/block"/*.bin >/dev/null 2>&1 || ls "$ENGINE_DIR/block/archive"/*.prerestore >/dev/null 2>&1); then
    echo "STEP2_RESTORE_DONE"
  else
    echo ""
  fi
}

state_label() {
  case "$1" in
    STEP1_DONE)         echo "STEP 1/3 — Boot Block captured (Next: STEP 2/3 yigitzra1n NAND Restore)" ;;
    STEP2_RESTORE_DONE) echo "STEP 2/3 — yigitzra1n NAND Restore finished (Next: STEP 3/3 First Boot)" ;;
    *)                  echo "$1" ;;
  esac
}

get_irec_mode() {
  local irec
  irec="$(find_irecovery_bin)"
  python3 - "$irec" <<'PY' 2>/dev/null || true
import subprocess, sys
try:
    r = subprocess.run([sys.argv[1], "-q"], capture_output=True, text=True, timeout=1.2)
    for line in r.stdout.splitlines():
        if line.startswith("MODE:"):
            print(line.split(":", 1)[1].strip())
            break
except Exception:
    pass
PY
}

is_dfu_mode() {
  [[ "$(get_irec_mode)" == "DFU" ]]
}

maybe_enter_recovery_from_ios() {
  command -v idevice_id >/dev/null 2>&1 || return 0
  command -v ideviceenterrecovery >/dev/null 2>&1 || return 0

  local udid=""
  udid="$(idevice_id -l 2>/dev/null | head -n 1 | tr -d '\r' || true)"
  [[ -n "$udid" ]] || return 0
  if [[ "$udid" == *SSHRD* || "$udid" == ffffffff* ]]; then
    return 0
  fi

  echo "  [*] Booted iOS detected ($udid) — entering Recovery mode..."
  if ideviceenterrecovery "$udid" >/dev/null 2>&1; then
    local w
    for w in $(seq 1 15); do
      sleep 1
      if [[ "$(get_irec_mode)" == "Recovery" ]]; then
        return 0
      fi
    done
  else
    echo "  [!] Could not auto-enter Recovery (unlock phone & tap Trust if prompted, or enter DFU manually)."
  fi
  return 0
}

try_confirm_dfu() {
  local cpid_rc=0
  check_device_cpid "$FORCE" || cpid_rc=$?
  if [[ "$cpid_rc" -eq 0 ]]; then
    return 0
  elif [[ "$cpid_rc" -eq 2 ]]; then
    exit 1
  fi
  echo "  [!] DFU handle dropped or stalled (half-DFU). Hold POWER + HOME for ~10s to reset, then enter DFU again."
  sleep 1
  return 1
}

prompt_dfu() {
  local step_title="$1"
  local irec
  irec="$(find_irecovery_bin)"

  while true; do
    echo ""
    echo "======================================================================"
    echo "  yigitzra1n :: $step_title"
    echo "======================================================================"

    if is_dfu_mode; then
      echo "  [+] Device is already in DFU mode."
      if try_confirm_dfu; then
        return 0
      fi
      continue
    fi

    local cur_mode
    cur_mode="$(get_irec_mode)"
    if [[ -z "$cur_mode" ]]; then
      maybe_enter_recovery_from_ios
      cur_mode="$(get_irec_mode)"
    fi
    local in_recovery=0
    if [[ "$cur_mode" == "Recovery" ]]; then
      in_recovery=1
      "$irec" -c "setenv auto-boot true" >/dev/null 2>&1 || true
      "$irec" -c "saveenv" >/dev/null 2>&1 || true
      echo "  [+] Recovery mode detected (auto-reboot trigger armed at 2s if you use Guided DFU)."
    else
      echo "  [*] Device state: ${cur_mode:-waiting for USB} (Recovery mode is NOT required)."
    fi

    echo ""
    echo "  [NOTE] Avoid USB-C to Lightning cables on A9 — use USB-A to Lightning (+ adapter if needed)."
    echo ""
    echo "  Waiting for DFU mode — choose either method:"
    echo "    • Put your iPhone 6s into DFU directly at any time (auto-detected immediately!)"
    echo "    • Or press [Enter] for Guided DFU countdown (4s POWER+HOME -> 10s HOME), or 'q' to abort."
    echo ""

    local wait_rc=0
    python3 -c '
import os, select, subprocess, sys, time

irec = sys.argv[1]
sys.stdout.write("  Waiting for DFU (or press [Enter] for Guided DFU, '\''q'\'' + Enter to abort)... ")
sys.stdout.flush()

try:
    tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
except OSError:
    tty_fd = sys.stdin.fileno()

last_poll = 0.0
try:
    while True:
        now = time.monotonic()
        if now - last_poll >= 0.7:
            last_poll = now
            try:
                r = subprocess.run([irec, "-q"], capture_output=True, text=True, timeout=0.6)
                for line in r.stdout.splitlines():
                    if line.startswith("MODE:") and line.split(":", 1)[1].strip() == "DFU":
                        sys.stdout.write("\n  [+] DFU mode auto-detected!\n")
                        sys.stdout.flush()
                        sys.exit(0)
            except Exception:
                pass
        ready, _, _ = select.select([tty_fd], [], [], 0.2)
        if ready:
            try:
                data = os.read(tty_fd, 64)
            except BlockingIOError:
                continue
            if not data:
                continue
            txt = data.decode("utf-8", errors="ignore").strip().lower()
            if txt.startswith("q"):
                sys.exit(130)
            sys.exit(10)
finally:
    if tty_fd != sys.stdin.fileno():
        try:
            os.close(tty_fd)
        except OSError:
            pass
' "$irec" || wait_rc=$?
    if [[ "$wait_rc" -eq 130 ]]; then
      echo "[*] Aborted by user."
      exit 130
    elif [[ "$wait_rc" -eq 0 ]]; then
      if try_confirm_dfu; then
        return 0
      fi
      continue
    fi

    cur_mode="$(get_irec_mode)"
    if [[ "$cur_mode" == "DFU" ]]; then
      echo "  [+] DFU mode detected!"
      if try_confirm_dfu; then
        return 0
      fi
      continue
    fi
    if [[ -z "$cur_mode" ]]; then
      maybe_enter_recovery_from_ios
      cur_mode="$(get_irec_mode)"
    fi
    if [[ "$cur_mode" == "Recovery" ]]; then
      in_recovery=1
      "$irec" -c "setenv auto-boot true" >/dev/null 2>&1 || true
      "$irec" -c "saveenv" >/dev/null 2>&1 || true
    else
      in_recovery=0
    fi

    local dfu_rc=0
    python3 - "$irec" "$in_recovery" <<'PY' || dfu_rc=$?
import subprocess, sys, time

irec = sys.argv[1]
in_recovery = int(sys.argv[2]) == 1

def query_mode(timeout=0.5):
    try:
        r = subprocess.run([irec, "-q"], capture_output=True, text=True, timeout=timeout)
        for line in r.stdout.splitlines():
            if line.startswith("MODE:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return ""

def sleep_until(t_target):
    rem = t_target - time.monotonic()
    if rem > 0:
        time.sleep(rem)

sys.stdout.write("  Get ready! Countdown starts in:\n")
sys.stdout.flush()
t0 = time.monotonic()
for idx, c in enumerate((3, 2, 1), start=1):
    sys.stdout.write(f"\r    {c}...   ")
    sys.stdout.flush()
    sleep_until(t0 + idx)
sys.stdout.write("\r    GO!    \n")
sys.stdout.flush()

sys.stdout.write("  STEP 1: Hold POWER + HOME together (4 sec)\n")
sys.stdout.flush()
t_step1 = time.monotonic()
reboot_fired = False
for sec in range(1, 5):
    bar = "=" * sec + " " * (4 - sec)
    rem = 4 - sec
    if sec == 2 and in_recovery and not reboot_fired:
        reboot_fired = True
        try:
            subprocess.Popen([irec, "-c", "reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
    sleep_until(t_step1 + sec)
    if rem > 0:
        sys.stdout.write(f"\r    [{bar}] ({sec}/4 sec — {rem}s left)   ")
    else:
        sys.stdout.write(f"\r    [{bar}] (4/4 sec — 0s left) -> RELEASE POWER NOW!\n")
    sys.stdout.flush()

sys.stdout.write("  STEP 2: Release POWER, keep holding HOME (10 sec)\n")
sys.stdout.flush()
t_step2 = time.monotonic()
for sec in range(1, 11):
    target = t_step2 + sec
    while time.monotonic() < target:
        if sec >= 2 and query_mode(timeout=0.45) == "DFU":
            bar = "=" * sec + " " * (10 - sec)
            rem = 10 - sec
            sys.stdout.write(f"\r    [{bar}] ({sec}/10 sec — {rem}s left)   \n")
            sys.stdout.write(f"  [+] DFU mode detected at {sec}s! You can release HOME.\n")
            sys.stdout.flush()
            sys.exit(0)
        sleep_rem = min(0.25, target - time.monotonic())
        if sleep_rem > 0:
            time.sleep(sleep_rem)
    bar = "=" * sec + " " * (10 - sec)
    rem = 10 - sec
    sys.stdout.write(f"\r    [{bar}] ({sec}/10 sec — {rem}s left)   ")
    sys.stdout.flush()
    if sec >= 2 and query_mode(timeout=0.45) == "DFU":
        sys.stdout.write(f"\n  [+] DFU mode detected at {sec}s! You can release HOME.\n")
        sys.stdout.flush()
        sys.exit(0)

sys.stdout.write("\n")
sys.stdout.flush()
sys.exit(1)
PY

    if [[ "$dfu_rc" -eq 0 ]] || is_dfu_mode; then
      if try_confirm_dfu; then
        return 0
      fi
      continue
    fi

    echo "  [!] DFU not detected within 10s — looping back to live DFU listener..."
  done
}

find_latest_block() {
  if [[ -n "$BLOCK_PATH" && -f "$BLOCK_PATH" ]]; then
    echo "$BLOCK_PATH"
    return 0
  fi
  local found=""
  for pat in "*pteblock*.bin" "*shcblock*.bin" "*.bin"; do
    found="$(ls -t "$ENGINE_DIR/block"/$pat 2>/dev/null | head -n 1 || true)"
    if [[ -n "$found" ]]; then
      echo "$found"
      return 0
    fi
  done
  return 1
}

cmd_prepare() {
  TELEMETRY_STEP="PREPARE_1_ENV"
  echo ">>> [yigitzra1n 1/7] Checking macOS host environment & Python venv..."
  check_macos_host
  check_cli_tools
  ensure_python_venv
  check_disk_space 30
  check_connected_device_early "$FORCE"

  TELEMETRY_STEP="PREPARE_2_ASSETS"
  echo ">>> [yigitzra1n 2/7] Fetching / verifying 3 stock Apple IPSWs & open-source tools..."
  local fetch_args=(--ipsw-dir "$IPSW_DIR")
  [[ "$SKIP_DOWNLOAD" -eq 1 ]] && fetch_args+=(--skip-download)
  bash "$ROOT_DIR/scripts/01_fetch_assets.sh" "${fetch_args[@]}"

  local ipsw_1588 ipsw_ipad5 ipsw_ip8
  ipsw_1588="$(find_ipsw "$IPSW_6S_1588_GLOB")"
  ipsw_ipad5="$(find_ipsw "$IPSW_IPAD5_16716_GLOB")"
  ipsw_ip8="$(find_ipsw "$IPSW_IP8_16716_GLOB")"

  TELEMETRY_STEP="PREPARE_3_BUILD"
  echo ">>> [yigitzra1n 3/7] Compiling host helpers, PongoOS hw_init + bootlogo module & iOS binaries..."
  bash "$ROOT_DIR/scripts/build_all.sh"

  TELEMETRY_STEP="PREPARE_4_ENGINE"
  echo ">>> [yigitzra1n 4/7] Preparing local restore/boot engine & extracting PongoOS modules..."
  telemetry_run_py "02_patch_engine.py" \
    --raw-dir "$ROOT_DIR/.cache/engine/raw" \
    --out-dir "$ENGINE_DIR" \
    --api-dir "$LOCAL_API_DIR" \
    --ipsw-1588 "$ipsw_1588"

  TELEMETRY_STEP="PREPARE_5_KERNEL_DT"
  echo ">>> [yigitzra1n 5/7] Building kernelcache v6 & DeviceTree v5 from stock IPSWs..."
  telemetry_run_py "03_build_kernel_v6.py" \
    --ipsw-1588 "$ipsw_1588" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out "$BUILD_DIR/kernelcache.release.n71_v6.im4p"

  telemetry_run_py "04_build_dtree_v5.py" \
    --ipsw-1588 "$ipsw_1588" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out "$BUILD_DIR/DeviceTree.n71map_v5.im4p" \
    --out-ramdisk "$BUILD_DIR/DeviceTree.n71map_ramdisk.im4p"

  telemetry_run_py "04_build_dtree_v5.py" \
    --ipsw-1588 "$ipsw_1588" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --board n71ap \
    --out "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" \
    --out-ramdisk "$BUILD_DIR/DeviceTree.n71ap_ramdisk.im4p"

  TELEMETRY_STEP="PREPARE_6_RAMDISK"
  echo ">>> [yigitzra1n 6/7] Building Restore RamDisk (14-patch) & Maintenance SSH RamDisk..."
  telemetry_run_py "05_build_ramdisk.py" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out-restore-rd "$BUILD_DIR/098-68700-067.dmg" \
    --out-restore-tc "$BUILD_DIR/098-68700-067.dmg.trustcache" \
    --procursus-dir "$PROCURSUS_DIR" \
    --ssh-rd-init "$BUILD_DIR/bin/ssh_rd_init" \
    --out-ssh-rd "$BUILD_DIR/ssh_ramdisk.im4p" \
    --out-ssh-tc "$BUILD_DIR/ssh_ramdisk.trustcache"

  TELEMETRY_STEP="PREPARE_7_ROOTFS"
  echo ">>> [yigitzra1n 7/7] Building yigitzra1n RootFS & staging restore directory (requires sudo)..."
  IPSW_DIR="$IPSW_DIR" bash "$ROOT_DIR/scripts/06_build_rootfs.sh"

  telemetry_send "install_success" "prepare" "PREPARE_DONE" "yigitzra1n.sh" "$LINENO" 0 "prepared" ""
  date +%s > "$ROOT_DIR/.yigitzra1n_installed" 2>/dev/null || true
  ensure_global_launcher_and_path "$ROOT_DIR" "${YIGITZRA1N_FALLBACK_SRC:-}"
  echo ""
  echo "[+] yigitzra1n preparation complete! Everything is verified in $BUILD_DIR."
  echo "    Next step: Ensure your iPhone 6s is on iOS 15.8.8, then run:"
  echo "      yigitzra1n install"
}

ensure_patched_engine() {
  local rev_file="$ENGINE_DIR/.patch_rev"
  if [[ -f "$rev_file" && "$(tr -d '[:space:]' < "$rev_file" 2>/dev/null || true)" == "v2_x86_64" ]]; then
    return 0
  fi
  local raw_dir="$ROOT_DIR/.cache/engine/raw"
  local f15=""
  f15="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_6S_1588_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -d "$raw_dir" && -n "$f15" ]]; then
    echo "[*] Updating engine patches (x86_64 + arm64)..."
    telemetry_run_py "02_patch_engine.py" \
      --raw-dir "$raw_dir" \
      --out-dir "$ENGINE_DIR" \
      --api-dir "$LOCAL_API_DIR" \
      --ipsw-1588 "$f15"
  fi
}

ensure_n71ap_devicetree() {
  local rev_file="$BUILD_DIR/.n71ap_dt_rev"
  local cur_rev=""
  [[ -f "$rev_file" ]] && cur_rev="$(tr -d '[:space:]' < "$rev_file" 2>/dev/null || true)"
  if [[ ! -f "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" || "$cur_rev" != "j71sap_v5" ]]; then
    local f15 f16
    f15="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_6S_1588_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
    f16="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_IPAD5_16716_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
    if [[ -n "$f15" && -n "$f16" ]]; then
      echo "[*] Building DeviceTree v5 for Samsung A9 (N71AP / J71sAP)..."
      telemetry_run_py "04_build_dtree_v5.py" \
        --ipsw-1588 "$f15" \
        --ipsw-ipad5 "$f16" \
        --board n71ap \
        --out "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" \
        --out-ramdisk "$BUILD_DIR/DeviceTree.n71ap_ramdisk.im4p"
      printf 'j71sap_v5\n' > "$rev_file"
    fi
  fi
  if [[ -f "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" ]]; then
    local d
    for d in "$BUILD_DIR/hybrid_ipsw/Firmware/all_flash" "$BUILD_DIR/ssh_ramdisk_ipsw/Firmware/all_flash"; do
      if [[ -d "$d" ]] && ! cmp -s "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" "$d/DeviceTree.n71ap.im4p"; then
        cp -f "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" "$d/DeviceTree.n71ap.im4p"
        echo "[+] Staged patched DeviceTree.n71ap.im4p in $d"
      fi
    done
  fi
}

collect_engine_failure_detail() {
  local dev_info=""
  local irec=""
  irec="$(command -v irecovery || echo /opt/homebrew/bin/irecovery)"
  [[ -x "$irec" ]] || irec="/usr/local/bin/irecovery"
  if [[ -x "$irec" ]]; then
    dev_info="$("$irec" -q 2>/dev/null | grep -E "^(CPID|BDID|MODE|PWND|SRTG):" | tr '\n' ' ' || true)"
  fi
  python3 - "$BUILD_DIR/engine_session.log" "$dev_info" <<'PY' 2>/dev/null || printf '%s' "${dev_info:-engine failed}"
import pathlib, re, sys
logp = pathlib.Path(sys.argv[1])
dev = sys.argv[2].strip()
tail = []
if logp.is_file():
    raw = logp.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", l).strip() for l in raw.splitlines()]
    lines = [l for l in lines if l and not l.startswith("Downloading ") and not l.endswith("%")]
    tail = lines[-14:]
msg = " | ".join(tail)
if dev:
    msg = f"[{dev}] {msg}" if msg else f"[{dev}]"
print(msg[-1150:])
PY
}

cmd_install() {
  TELEMETRY_STEP="INSTALL_PREFLIGHT"
  check_macos_host
  check_cli_tools
  ensure_python_venv
  [[ -d "$BUILD_DIR/hybrid_ipsw" && -f "$BUILD_DIR/hybrid_ipsw/098-68805-067.dmg" ]] || {
    echo "[ERROR] Build outputs missing. Run './yigitzra1n.sh prepare' first." >&2
    exit 1
  }
  ensure_patched_engine
  ensure_n71ap_devicetree
  local ipsw_1588=""
  ipsw_1588="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_6S_1588_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -z "$ipsw_1588" ]]; then
    echo "[ERROR] Missing stock iOS 15.8.8 IPSW in $IPSW_DIR. Run './yigitzra1n.sh prepare' first." >&2
    telemetry_send "install_fail" "install" "$TELEMETRY_STEP" "yigitzra1n.sh" "$LINENO" 1 "missing_1588_ipsw" "Missing iOS 15.8.8 IPSW in $IPSW_DIR"
    telemetry_mark_reported
    exit 1
  fi
  local ipsw_rc=0
  python3 "$ROOT_DIR/scripts/verify_ipsw.py" "$ipsw_1588" "iPhone 6s (15.8.8) IPSW" || ipsw_rc=$?
  if [[ "$ipsw_rc" -ne 0 ]]; then
    local ipsw_reason="invalid_ipsw_zip"
    [[ "$ipsw_rc" -eq 21 ]] && ipsw_reason="invalid_ipsw_alias"
    telemetry_send "install_fail" "install" "$TELEMETRY_STEP" "yigitzra1n.sh" "$LINENO" "$ipsw_rc" "$ipsw_reason" "Invalid iOS 15.8.8 IPSW: $ipsw_1588"
    telemetry_mark_reported
    exit 1
  fi
  ensure_local_server

  echo "======================================================================"
  echo "  yigitzra1n — iOS 16.7.16 Guided Installation (iPhone 6s N71mAP)"
  echo "======================================================================"
  echo "  WARNING: This erases all user data on the device."
  echo "  Prerequisite: Device must currently be running iOS 15.8.8 (19H422)."
  echo ""

  mkdir -p "$ENGINE_DIR/block" "$ENGINE_DIR/block/archive" "$ENGINE_DIR/image4"

  local cur_state=""
  cur_state="$(detect_install_state)"
  local skip_step1=0
  local skip_step2=0

  if [[ -n "$cur_state" ]]; then
    echo "[*] Found previous installation checkpoint:"
    echo "    $(state_label "$cur_state")"
    printf "    Resume from where you left off? [Y/n]: "
    local resume_ans=""
    read -r resume_ans
    if [[ "${resume_ans:-Y}" =~ ^[Nn]$ ]]; then
      rm -f "$INSTALL_STATE_FILE"
      cur_state=""
      echo "[*] Starting fresh from STEP 1/3."
    else
      case "$cur_state" in
        STEP1_DONE)
          skip_step1=1
          ;;
        STEP2_RESTORE_DONE|STEP3A_POST_SHC_DONE|STEP3B_PTE_DONE)
          skip_step1=1; skip_step2=1
          ;;
      esac
      echo "[+] Resuming from checkpoint: $cur_state"
    fi
  fi

  local boot_blk=""
  if [[ "$skip_step1" -eq 0 ]]; then
    if [[ -n "$BLOCK_PATH" && -f "$BLOCK_PATH" ]]; then
      boot_blk="$BLOCK_PATH"
    else
      boot_blk="$(find_latest_block 2>/dev/null || true)"
      if [[ -n "$boot_blk" ]]; then
        echo "[*] Found existing boot block in $ENGINE_DIR/block: $(basename "$boot_blk")"
        printf "    Reuse this block? [Y/n]: "
        read -r ans
        [[ "${ans:-Y}" =~ ^[Nn]$ ]] && boot_blk=""
      fi
    fi

    TELEMETRY_STEP="STEP1_SHC"
    while [[ -z "$boot_blk" ]]; do
      prompt_dfu "STEP 1/3 — Capture A9 Boot Block (DFU #1)"
      echo "[*] Entering pwnDFU..."
      if run_engine_clean ./bin/engine_helper -D; then
        echo "[*] Acquiring A9 SHC boot block from iOS 15.8.8..."
        if DYLD_INSERT_LIBRARIES="$BUILD_DIR/bin/fast_dns.dylib" \
          run_engine_clean ./bin/engine_restore -y --get-shcblock "$ipsw_1588"; then
          boot_blk="$(find_latest_block 2>/dev/null || true)"
        fi
      fi
      if [[ -z "$boot_blk" ]]; then
        local step1_err=""
        step1_err="$(collect_engine_failure_detail)"
        telemetry_send "install_fail" "install" "STEP1_SHC" "yigitzra1n.sh" "$LINENO" 1 "shc_capture_attempt_fail" "${step1_err:-SHC boot block capture failed}" 1
        echo "[!] STEP 1/3 did not produce a boot block." >&2
        printf "    Retry STEP 1/3? [Y/n]: "
        read -r retry_ans
        if [[ "${retry_ans:-Y}" =~ ^[Nn]$ ]]; then
          telemetry_send "install_fail" "install" "STEP1_SHC" "yigitzra1n.sh" "$LINENO" 1 "shc_capture_failed" "${step1_err:-SHC boot block capture failed}"
          telemetry_mark_reported
          exit 1
        fi
      else
        echo "[+] Captured boot block: $(basename "$boot_blk")"
        save_install_state "STEP1_DONE"
      fi
    done
  else
    boot_blk="$(find_latest_block 2>/dev/null || true)"
    if [[ -z "$boot_blk" ]]; then
      local arch_blk
      arch_blk="$(ls -t "$ENGINE_DIR/block/archive"/*.prerestore 2>/dev/null | head -n 1 || true)"
      if [[ -n "$arch_blk" ]]; then
        local dst_blk="$ENGINE_DIR/block/$(basename "${arch_blk%.prerestore}")"
        cp -f "$arch_blk" "$dst_blk"
        boot_blk="$dst_blk"
        echo "[+] Restored boot block from archive: $(basename "$boot_blk")"
      fi
    fi
  fi

  if [[ "$skip_step2" -eq 0 ]]; then
    TELEMETRY_STEP="STEP2_RESTORE"
    local restore_blk_flag="--load-pteblock"
    [[ "$(basename "$boot_blk" | tr '[:upper:]' '[:lower:]')" == *shcblock* ]] && restore_blk_flag="--load-shcblock"

    while true; do
      prompt_dfu "STEP 2/3 — Write yigitzra1n to NAND (DFU #2)"
      echo "[*] Entering pwnDFU..."
      if run_engine_clean ./bin/engine_helper -D; then
        echo "[*] Starting NAND restore (if exploit pauses >10s, briefly replug USB for 1 second)..."
        if DYLD_INSERT_LIBRARIES="$BUILD_DIR/bin/fast_dns.dylib" \
          run_engine_clean ./bin/engine_restore -y -o --allow-unsupport "$restore_blk_flag" "$boot_blk" "$BUILD_DIR/hybrid_ipsw"; then
          echo "[+] yigitzra1n NAND restore completed!"
          break
        fi
      fi
      local step2_err=""
      step2_err="$(collect_engine_failure_detail)"
      telemetry_send "install_fail" "install" "STEP2_RESTORE" "yigitzra1n.sh" "$LINENO" 1 "nand_restore_attempt_fail" "${step2_err:-yigitzra1n NAND restore failed}" 1
      echo "[!] STEP 2/3 (NAND restore) failed." >&2
      printf "    Retry STEP 2/3? [Y/n]: "
      read -r retry_ans
      if [[ "${retry_ans:-Y}" =~ ^[Nn]$ ]]; then
        telemetry_send "install_fail" "install" "STEP2_RESTORE" "yigitzra1n.sh" "$LINENO" 1 "nand_restore_failed" "${step2_err:-yigitzra1n NAND restore failed}"
        telemetry_mark_reported
        exit 1
      fi
    done

    save_install_state "STEP2_RESTORE_DONE"
  fi

  TELEMETRY_STEP="STEP3_BOOT"
  echo ""
  echo "[+] yigitzra1n installed on NAND and your boot block is saved:"
  echo "    ${boot_blk:-$ENGINE_DIR/block/}"
  echo "    Back it up anytime with: yigitzra1n export-block"
  echo ""
  printf "STEP 3/3 — Boot into iOS 16.7.16 now? [Y/n]: "
  read -r boot_ans
  if [[ ! "${boot_ans:-Y}" =~ ^[Nn]$ ]]; then
    BLOCK_PATH="$boot_blk" cmd_boot
  fi
  telemetry_send "install_success" "install" "INSTALL_DONE" "yigitzra1n.sh" "$LINENO" 0 "installed" ""
  date +%s > "$ROOT_DIR/.yigitzra1n_installed" 2>/dev/null || true
  ensure_global_launcher_and_path "$ROOT_DIR" "${YIGITZRA1N_FALLBACK_SRC:-}"
}

cmd_boot() {
  TELEMETRY_STEP="BOOT"
  check_macos_host
  check_cli_tools
  ensure_python_venv
  [[ -d "$BUILD_DIR/hybrid_ipsw" && -f "$BUILD_DIR/hybrid_ipsw/098-68805-067.dmg" && -f "$BUILD_DIR/bin/hw_init_probe.bin" ]] || {
    echo "[ERROR] Build outputs missing (or incomplete hybrid_ipsw/098-68805-067.dmg). Run './yigitzra1n.sh prepare' first." >&2
    exit 1
  }
  ensure_patched_engine
  ensure_n71ap_devicetree
  if [[ "$SKIP_SILEO" -eq 0 && "${YIGITZRA1N_UPDATED:-0}" != "1" ]]; then
    print_legacy_sileo_notice
  fi
  prompt_dfu "Tethered Boot — iOS 16.7.16 (DFU Mode)"
  local py_args=()
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ -n "$BOOT_ARGS" ]] && py_args+=(--boot-args "$BOOT_ARGS")
  [[ "$QUIET" -eq 1 ]] && py_args+=(--quiet)
  [[ "$FORCE" -eq 1 || "${YIGITZRA1N_S8000_CONFIRMED:-0}" == "1" ]] && py_args+=(--force)
  YIGITZRA1N_IPSW_DIR="$IPSW_DIR" telemetry_run_py "pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}
  if [[ "$TELEMETRY_CMD" == "boot" ]]; then
    telemetry_send "install_success" "boot" "BOOT_DONE" "yigitzra1n.sh" "$LINENO" 0 "booted" ""
  fi
  local no_boot_flag=0
  for _a in ${py_args[@]+"${py_args[@]}"}; do
    [[ "$_a" == "--no-boot" ]] && no_boot_flag=1
  done
  if [[ "$no_boot_flag" -eq 0 ]]; then
    if [[ "$SKIP_SILEO" -eq 0 ]]; then
      (
        set +e
        trap - ERR
        PROCURSUS_DIR="$PROCURSUS_DIR" SILEO_REQUIRE_SSH=0 bash "$ROOT_DIR/scripts/provision_sileo.sh"
      ) || echo "[!] Non-fatal Sileo check/install skipped; boot succeeded." >&2
    fi
    (
      set +e
      trap - ERR
      cam_wait=45
      [[ "$SKIP_SILEO" -eq 0 ]] && cam_wait=15
      python3 "$ROOT_DIR/scripts/apply_camera_fix.py" --wait-tries "$cam_wait"
    ) || true
  fi
}

cmd_ssh_ramdisk() {
  TELEMETRY_STEP="SSH_RAMDISK"
  check_macos_host
  check_cli_tools
  ensure_python_venv
  [[ -d "$BUILD_DIR/ssh_ramdisk_ipsw" && -f "$BUILD_DIR/bin/hw_init_probe.bin" ]] || {
    echo "[ERROR] SSH RamDisk outputs missing. Run './yigitzra1n.sh prepare' first." >&2
    exit 1
  }
  ensure_patched_engine
  ensure_n71ap_devicetree
  prompt_dfu "Maintenance SSH RamDisk Boot (DFU Mode)"
  local py_args=(--ssh-ramdisk)
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ "$FORCE" -eq 1 || "${YIGITZRA1N_S8000_CONFIRMED:-0}" == "1" ]] && py_args+=(--force)
  telemetry_run_py "pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}

  echo ""
  echo "[*] Starting USB port forward (iproxy 2222 -> 22)..."
  echo "    Wait ~10 seconds for RamDisk boot & NAND mounts (/mnt1 = System, /mnt2 = Data), then connect:"
  echo "      ssh root@127.0.0.1 -p 2222   (default password: alpine)"
  echo "    Press Ctrl+C to stop iproxy."
  exec iproxy 2222 22
}

cmd_uninstall() {
  TELEMETRY_STEP="UNINSTALL_RAMDISK"
  check_macos_host
  check_cli_tools
  ensure_python_venv
  [[ -d "$BUILD_DIR/ssh_ramdisk_ipsw" && -f "$BUILD_DIR/bin/hw_init_probe.bin" ]] || {
    echo "[ERROR] SSH RamDisk outputs missing. Run './yigitzra1n.sh prepare' first." >&2
    exit 1
  }
  ensure_patched_engine
  ensure_n71ap_devicetree

  echo "======================================================================"
  echo "  yigitzra1n — Uninstall / Prepare Device for Stock iOS 15.8.8 Restore"
  echo "======================================================================"
  echo "  This boots the Maintenance SSH RamDisk and resets ONLY the APFS"
  echo "  container (/dev/disk0s1) so stock iOS 15.8.8 restores cleanly without"
  echo "  Error 9 ('unencrypted data volume is not allowed')."
  echo "  Raw /dev/disk0, NOR, and Syscfg are NEVER modified."
  echo ""
  echo "  WARNING: ALL data on the device will be erased!"
  printf "  Type YES to continue (anything else aborts): "
  local confirm=""
  read -r confirm || true
  if [[ "$confirm" != "YES" ]]; then
    echo "[*] Aborted."
    exit 0
  fi

  local uninstall_reason=""
  echo ""
  echo "  Please write why you are uninstalling iOS 16.7.16 (required):"
  echo "  (e.g. battery drain, camera/audio issue, boot problem, just testing...)"
  while true; do
    printf "  Reason (min 4 chars): "
    if ! IFS= read -r uninstall_reason; then
      echo ""
      echo "[*] Uninstall aborted (reason is required)." >&2
      exit 0
    fi
    local trimmed_reason=""
    trimmed_reason="$(printf '%s' "$uninstall_reason" | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')"
    if (( ${#trimmed_reason} >= 4 )); then
      uninstall_reason="$trimmed_reason"
      break
    fi
    echo "  [!] Please enter at least 4 characters." >&2
  done

  TELEMETRY_STEP="UNINSTALL_REASON"
  telemetry_send "uninstall" "uninstall" "UNINSTALL_REASON" "yigitzra1n.sh" "$LINENO" 0 "user_uninstall_reason" "$uninstall_reason" 1
  TELEMETRY_STEP="UNINSTALL_RAMDISK"

  prompt_dfu "Uninstall / Reset APFS — Maintenance SSH RamDisk (DFU Mode)"
  local py_args=(--ssh-ramdisk)
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ "$FORCE" -eq 1 || "${YIGITZRA1N_S8000_CONFIRMED:-0}" == "1" ]] && py_args+=(--force)
  telemetry_run_py "pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}

  local iproxy_pid=""
  local ssh_port="$DEVICE_PORT"
  cleanup_uninstall() {
    local rc=$?
    if [[ -n "$iproxy_pid" ]]; then
      kill "$iproxy_pid" 2>/dev/null || true
      wait "$iproxy_pid" 2>/dev/null || true
    fi
    telemetry_on_exit "$TELEMETRY_CMD" "$TELEMETRY_STEP" "$rc"
  }
  trap cleanup_uninstall EXIT

  while lsof -iTCP:"$ssh_port" -sTCP:LISTEN >/dev/null 2>&1; do
    ssh_port=$((ssh_port + 1))
  done
  echo "[*] Starting USB port forward (127.0.0.1:${ssh_port} -> :22)..."
  if iproxy --help 2>&1 | grep -q "LOCAL_PORT:DEVICE_PORT"; then
    iproxy "${ssh_port}:22" >/dev/null 2>&1 &
    iproxy_pid=$!
  else
    iproxy "$ssh_port" 22 >/dev/null 2>&1 &
    iproxy_pid=$!
  fi

  echo "[*] Waiting for Maintenance SSH RamDisk on 127.0.0.1:${ssh_port} (up to 130s)..."
  echo "[!] Note: Booting the SSH RamDisk can take up to ~70 seconds. Please do NOT unplug the USB cable!"
  local ssh_opts=(
    -o LogLevel=ERROR
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o PubkeyAuthentication=no
    -o PreferredAuthentications=password
    -o ConnectTimeout=2
    -p "$ssh_port"
  )
  local ready=0
  local elapsed=0
  local tick_start=0
  for elapsed in $(seq 1 130); do
    tick_start=$SECONDS
    if sshpass -p "${SSH_PASS:-alpine}" ssh "${ssh_opts[@]}" root@127.0.0.1 "echo READY" 2>/dev/null | grep -q "READY"; then
      ready=1
      printf "\r    [+] SSH RamDisk ready after %ds.                     \n" "$elapsed"
      break
    fi
    printf "\r    Waiting for SSH RamDisk... (%d/130s)   " "$elapsed"
    if (( SECONDS - tick_start < 1 )); then
      sleep 1
    fi
  done
  if [[ "$ready" -ne 1 ]]; then
    echo ""
    echo "[ERROR] Could not connect to Maintenance SSH RamDisk within 130s." >&2
    telemetry_send "install_fail" "uninstall" "UNINSTALL_SSH_WAIT" "yigitzra1n.sh" "$LINENO" 1 "ssh_ramdisk_timeout" "SSH RamDisk not ready within 130s"
    telemetry_mark_reported
    exit 1
  fi

  TELEMETRY_STEP="UNINSTALL_RESET_APFS"

  echo "[*] Connected! Resetting APFS container (/dev/disk0s1)..."
  sshpass -p "${SSH_PASS:-alpine}" ssh "${ssh_opts[@]}" root@127.0.0.1 '
    set -e
    export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    if [ -x /usr/local/bin/rd_reset_apfs ]; then
      /usr/local/bin/rd_reset_apfs --yes
    else
      /sbin/umount -f /mnt2 2>/dev/null || true
      /sbin/umount -f /mnt1 2>/dev/null || true
      /sbin/newfs_apfs -v Clean /dev/disk0s1
      sync
    fi
  '
  echo "[*] APFS reset succeeded! Rebooting device into Recovery mode..."
  sshpass -p "${SSH_PASS:-alpine}" ssh "${ssh_opts[@]}" root@127.0.0.1 '/sbin/reboot' 2>/dev/null || true

  cleanup_uninstall
  trap 'telemetry_on_exit "$TELEMETRY_CMD" "$TELEMETRY_STEP" "$?"' EXIT
  rm -f "$INSTALL_STATE_FILE"
  telemetry_send "uninstall" "uninstall" "UNINSTALL_DONE" "yigitzra1n.sh" "$LINENO" 0 "uninstalled" "$uninstall_reason" 1
  sleep 5

  echo ""
  echo "[+] APFS container reset complete! Your iPhone 6s is now ready for stock iOS 15.8.8."
  local ipsw_1588=""
  ipsw_1588="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_6S_1588_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$ipsw_1588" && -f "$ipsw_1588" ]] && command -v idevicerestore >/dev/null 2>&1; then
    echo "    Detected local stock IPSW: $ipsw_1588"
    printf "    Start stock iOS 15.8.8 restore now via idevicerestore? [y/N]: "
    local rest_ans=""
    read -r rest_ans || true
    if [[ "$rest_ans" =~ ^[Yy]([Ee][Ss])?$ ]]; then
      TELEMETRY_STEP="UNINSTALL_RESTORE"
      mkdir -p "$BUILD_DIR/.tmp/restore_tmp" "$BUILD_DIR/.tmp/restore_cache"
      TMPDIR="$BUILD_DIR/.tmp/restore_tmp" idevicerestore -e -C "$BUILD_DIR/.tmp/restore_cache" "$ipsw_1588"
      telemetry_send "uninstall" "uninstall" "UNINSTALL_RESTORE_DONE" "yigitzra1n.sh" "$LINENO" 0 "restored_15_8_8" "$uninstall_reason" 1
      return 0
    fi
  fi
  echo "    Restore stock iOS 15.8.8 using Finder/iTunes (Option + Restore) or:"
  echo "      idevicerestore -e ${ipsw_1588:-ipsw/iPhone_4.7_15.8.8_19H422_Restore.ipsw}"
}

cmd_patch_device() {
  TELEMETRY_STEP="PATCH_DEVICE"
  ensure_python_venv
  local aop_arg=()
  if [[ -f "$BUILD_DIR/hybrid_ipsw/Firmware/AOP/aopfw-s8000aop.im4p" ]]; then
    "$VENV_PY" - "$BUILD_DIR/hybrid_ipsw/Firmware/AOP/aopfw-s8000aop.im4p" "$BUILD_DIR/s8000aop.bin" <<'PY'
import pyimg4, sys
im = pyimg4.IM4P(open(sys.argv[1], "rb").read())
if im.payload.compression != pyimg4.Compression.NONE:
    im.payload.decompress()
open(sys.argv[2], "wb").write(im.payload.data)
PY
    aop_arg=(--aop-bin "$BUILD_DIR/s8000aop.bin")
  fi

  local patch_iproxy_pid=""
  cleanup_patch_device() {
    if [[ -n "$patch_iproxy_pid" ]]; then
      kill "$patch_iproxy_pid" 2>/dev/null || true
      wait "$patch_iproxy_pid" 2>/dev/null || true
      patch_iproxy_pid=""
    fi
  }
  trap 'rc=$?; cleanup_patch_device; telemetry_on_exit "$TELEMETRY_CMD" "$TELEMETRY_STEP" "$rc"' EXIT
  if [[ "$DEVICE_HOST" == "127.0.0.1" || "$DEVICE_HOST" == "localhost" ]]; then
    if ! lsof -iTCP:"$DEVICE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
      if command -v iproxy >/dev/null 2>&1; then
        if iproxy --help 2>&1 | grep -q "LOCAL_PORT:DEVICE_PORT"; then
          iproxy "${DEVICE_PORT}:22" >/dev/null 2>&1 &
        else
          iproxy "$DEVICE_PORT" 22 >/dev/null 2>&1 &
        fi
        patch_iproxy_pid=$!
        sleep 1
      fi
    fi
  fi

  DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$DEVICE_PORT" \
    bash "$ROOT_DIR/scripts/apply_device_patches.sh" ${aop_arg[@]+"${aop_arg[@]}"}

  if [[ -n "$IC_INFO" && -f "$IC_INFO" ]]; then
    echo "[*] Installing device FairPlay IC-Info.sisv..."
    local ic_ssh_opts=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o PreferredAuthentications=password -o ConnectTimeout=5 -p "$DEVICE_PORT")
    local ic_rc=0
    push_ic_info_ssh() {
      sshpass -p "${SSH_PASS:-alpine}" ssh "${ic_ssh_opts[@]}" "root@${DEVICE_HOST}" '
        set -e
        mkdir -p /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes
        cat > /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes/IC-Info.sisv
        chown -R mobile:mobile /private/var/mobile/Library/FairPlay
        chmod 755 /private/var/mobile/Library/FairPlay /private/var/mobile/Library/FairPlay/iTunes_Control /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes
        chmod 644 /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes/IC-Info.sisv
        killall -9 fairplayd.A2 2>/dev/null || true
      ' < "$IC_INFO"
    }
    push_ic_info_ssh || ic_rc=$?
    if [[ "$ic_rc" -eq 255 ]]; then
      for _w in {1..5}; do
        sleep 2
        if sshpass -p "${SSH_PASS:-alpine}" ssh "${ic_ssh_opts[@]}" "root@${DEVICE_HOST}" "true" >/dev/null 2>&1; then
          ic_rc=0
          push_ic_info_ssh || ic_rc=$?
          break
        fi
      done
    fi
    if [[ "$ic_rc" -ne 0 ]]; then
      echo "[ERROR] Failed installing IC-Info.sisv (SSH exit $ic_rc)." >&2
      exit "$ic_rc"
    fi
    echo "[+] IC-Info.sisv installed."
  fi
  cleanup_patch_device
  trap 'telemetry_on_exit "$TELEMETRY_CMD" "$TELEMETRY_STEP" "$?"' EXIT
  telemetry_send "install_success" "patch-device" "PATCH_DEVICE_DONE" "yigitzra1n.sh" "$LINENO" 0 "patched" ""
}

cmd_activate() {
  TELEMETRY_STEP="ACTIVATE"
  DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$DEVICE_PORT" \
    bash "$ROOT_DIR/scripts/activate_device.sh"
}

cmd_sileo() {
  TELEMETRY_STEP="SILEO"
  check_macos_host
  ensure_python_venv
  local sileo_port=""
  [[ "$PORT_EXPLICIT" -eq 1 ]] && sileo_port="$DEVICE_PORT"
  SILEO_REQUIRE_SSH=1 DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$sileo_port" PROCURSUS_DIR="$PROCURSUS_DIR" \
    bash "$ROOT_DIR/scripts/provision_sileo.sh"
  telemetry_send "install_success" "sileo" "SILEO_DONE" "yigitzra1n.sh" "$LINENO" 0 "sileo_ready" ""
}

cmd_message() {
  TELEMETRY_STEP="MESSAGE"
  local msg_text=""
  if (( ${#MESSAGE_PARTS[@]} > 0 )); then
    msg_text="${MESSAGE_PARTS[*]}"
  elif [[ -t 0 ]]; then
    echo "======================================================================"
    echo "  Send a Direct Message to Yigit (bug report, result, or feedback)"
    echo "======================================================================"
    echo "  I read every message! Max 600 characters (empty input cancels)."
    printf "  Message: "
    IFS= read -r msg_text || true
  else
    msg_text="$(cat)"
  fi

  if [[ -z "${msg_text//[[:space:]]/}" ]]; then
    echo "[*] Cancelled (empty message)."
    return 0
  fi
  telemetry_send_message "$msg_text" "cli"
}

cmd_messages() {
  TELEMETRY_STEP="MESSAGES"
  telemetry_fetch_messages
}

cmd_clean() {
  echo "[*] Removing $BUILD_DIR (keeping ipsw/ and .cache/)..."
  rm -rf "$BUILD_DIR"
  echo "[+] Clean complete."
}

cmd_doctor() {
  TELEMETRY_STEP="DOCTOR"
  local doc_rc=0
  python3 "$ROOT_DIR/scripts/doctor.py" --root "$ROOT_DIR" --ipsw-dir "$IPSW_DIR" --caller-pwd "$CALLER_PWD" || doc_rc=$?
  telemetry_mark_reported
  return "$doc_rc"
}

cmd_export_block() {
  TELEMETRY_STEP="EXPORT_BLOCK"
  local target="${OUT_PATH:-$POS_ARG}"
  python3 "$ROOT_DIR/scripts/shc_block.py" export \
    --engine-dir "$ENGINE_DIR" \
    --caller-pwd "$CALLER_PWD" \
    --block "$BLOCK_PATH" \
    --target "$target"
}

cmd_import_block() {
  TELEMETRY_STEP="IMPORT_BLOCK"
  local src="${POS_ARG:-$BLOCK_PATH}"
  local args=(import --engine-dir "$ENGINE_DIR" --caller-pwd "$CALLER_PWD" --target "$src")
  if [[ "$FORCE" -eq 1 ]]; then args+=(--force); fi
  python3 "$ROOT_DIR/scripts/shc_block.py" "${args[@]}"
}

cmd_panic_check() {
  TELEMETRY_STEP="PANIC_CHECK"
  check_macos_host
  local p_port=0
  if [[ "$PORT_EXPLICIT" -eq 1 ]]; then p_port="$DEVICE_PORT"; fi
  local dest="${OUT_PATH:-$POS_ARG}"
  python3 "$ROOT_DIR/scripts/panic_tools.py" check \
    --caller-pwd "$CALLER_PWD" \
    --out "$dest" \
    --host "$DEVICE_HOST" \
    --port "$p_port" \
    --url "${YIGITZRA1N_PANIC_URL:-${YIGITZRA1N_TELEMETRY_URL%/v1/event}/v1/panic}" \
    --tool-version "$(telemetry_effective_version)"
}

cmd_panic_send() {
  TELEMETRY_STEP="PANIC_SEND"
  check_macos_host
  local p_port=0
  if [[ "$PORT_EXPLICIT" -eq 1 ]]; then p_port="$DEVICE_PORT"; fi
  local args=(
    send
    --caller-pwd "$CALLER_PWD"
    --host "$DEVICE_HOST"
    --port "$p_port"
    --url "${YIGITZRA1N_PANIC_URL:-${YIGITZRA1N_TELEMETRY_URL%/v1/event}/v1/panic}"
    --tool-version "$(telemetry_effective_version)"
  )
  if [[ "$ASSUME_YES" -eq 1 || "$FORCE" -eq 1 ]]; then args+=(--yes); fi
  python3 "$ROOT_DIR/scripts/panic_tools.py" "${args[@]}"
}

cmd_sideload() {
  TELEMETRY_STEP="SIDELOAD"
  check_macos_host
  local p_port=0
  if [[ "$PORT_EXPLICIT" -eq 1 ]]; then p_port="$DEVICE_PORT"; fi
  local args=(--caller-pwd "$CALLER_PWD" --host "$DEVICE_HOST" --port "$p_port")
  if [[ "$LIST_MODE" -eq 1 ]]; then args+=(--list); fi
  if [[ -n "$UNINSTALL_TARGET" ]]; then args+=(--uninstall "$UNINSTALL_TARGET"); fi
  if [[ -n "$POS_ARG" ]]; then args+=("$POS_ARG"); fi
  python3 "$ROOT_DIR/scripts/sideload_ipa.py" "${args[@]}"
}

case "$CMD" in
  doctor)       cmd_doctor ;;
  prepare)      cmd_prepare ;;
  install)      cmd_install ;;
  boot)         cmd_boot ;;
  sileo)        cmd_sileo ;;
  sideload)     cmd_sideload ;;
  panic-check)  cmd_panic_check ;;
  panic-send)   cmd_panic_send ;;
  export-block) cmd_export_block ;;
  import-block) cmd_import_block ;;
  ssh-ramdisk)  cmd_ssh_ramdisk ;;
  uninstall)    cmd_uninstall ;;
  activate)     cmd_activate ;;
  patch-device) cmd_patch_device ;;
  message)      cmd_message ;;
  messages)     cmd_messages ;;
  clean)        cmd_clean ;;
  *) echo "[ERROR] Unknown command: $CMD" >&2; usage; exit 2 ;;
esac
