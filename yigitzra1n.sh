#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
BLOCK_PATH=""
BOOT_ARGS=""
DEVICE_HOST="127.0.0.1"
DEVICE_PORT="2222"
IC_INFO=""

usage() {
  cat <<'EOF'
yigitzra1n — iOS 16.7.16 (20H392) for iPhone 6s (N71mAP / TSMC s8003)

Prerequisite: Your iPhone 6s must be running iOS 15.8.8 (19H422) before installation.

Commands:
  prepare       Download (or reuse from ipsw/, symlinks supported) the 3 stock Apple IPSWs
                and build all patched images (kernel v6, DeviceTree v5, RamDisks, Hybrid RootFS).
  install       Interactive guided restore & permanent boot-block setup (prompts for DFU mode).
  boot          Tethered boot an installed iPhone 6s into iOS 16.7.16 (verbose logs + centered
                yigitzra1n logo by default; pass --quiet to hide kernel text).
  ssh-ramdisk   Boot the maintenance OpenSSH RamDisk (port 22 over USB via iproxy).
  uninstall     Reset the unencrypted iOS 16 APFS container (/dev/disk0s1) via SSH RamDisk so
                stock iOS 15.8.8 restores cleanly without iTunes/Finder Error 9.
  activate      Activate device & complete initial setup over SSH/USB.
  patch-device  Apply/refresh live userland patches over SSH (optional --ic-info).
  clean         Remove build/ artifacts on your Mac (keeps ipsw/ and .cache/; does not touch device).

Options:
  --ipsw-dir DIR     Custom directory containing the 3 stock Apple IPSWs (default: ./ipsw)
  --skip-download    Fail instead of downloading missing IPSWs/assets
  --force            Override TSMC s8003 (0x8003) hardware guard (experimental for Samsung s8000)
  --quiet, -q        Disable verbose kernel boot console (-v)
  --verbose          Explicitly enable verbose kernel boot console (-v, default)
  --block PATH       Explicit path to device boot block (.bin)
  --boot-args STR    Custom kernel boot-args for `boot`
  --host IP          SSH host for `patch-device` / `activate` (default: 127.0.0.1)
  --port PORT        SSH port for `patch-device` / `activate` (default: 2222)
  --ic-info PATH     Install your device's FairPlay IC-Info.sisv during `patch-device`
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
    --ipsw-dir) IPSW_DIR="$2"; shift 2 ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    --force) FORCE=1; shift ;;
    --quiet|-q) QUIET=1; shift ;;
    --verbose) QUIET=0; shift ;;
    --block) BLOCK_PATH="$2"; shift 2 ;;
    --boot-args) BOOT_ARGS="$2"; shift 2 ;;
    --host) DEVICE_HOST="$2"; shift 2 ;;
    --port) DEVICE_PORT="$2"; shift 2 ;;
    --ic-info) IC_INFO="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] Unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

export TELEMETRY_CMD="$CMD"
export TELEMETRY_STEP="INIT"
export TELEMETRY_PARENT=1
export TELEMETRY_MARKER_FILE="$BUILD_DIR/.tmp/.telemetry_reported"
rm -f "$TELEMETRY_MARKER_FILE" 2>/dev/null || true
telemetry_track_first_run "$ROOT_DIR" "$CMD"
trap 'telemetry_trap_err "$TELEMETRY_CMD" "$TELEMETRY_STEP" "yigitzra1n.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR

find_ipsw() {
  local pat="$1"
  local f
  f="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$pat" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  [[ -n "$f" ]] || { echo "[ERROR] Missing IPSW matching $pat in $IPSW_DIR" >&2; exit 1; }
  echo "$f"
}

ensure_local_server() {
  if ! lsof -i :8765 >/dev/null 2>&1; then
    echo "[*] Starting local firmware metadata server (127.0.0.1:8765)..."
    mkdir -p "$BUILD_DIR"
    nohup python3 "$ROOT_DIR/src/local_server.py" --root "$LOCAL_API_DIR" \
      >> "$BUILD_DIR/local_server.log" 2>&1 &
    sleep 0.6
  fi
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
    STEP1_DONE)         echo "STEP 1/3 — Boot Block captured (Next: STEP 2/3 Hybrid iOS 16.7.16 NAND Restore)" ;;
    STEP2_RESTORE_DONE) echo "STEP 2/3 — Hybrid iOS 16.7.16 NAND Restore finished (Next: STEP 3/3 First Boot)" ;;
    *)                  echo "$1" ;;
  esac
}

get_irec_mode() {
  local irec
  irec="$(command -v irecovery || echo /opt/homebrew/bin/irecovery)"
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
  irec="$(command -v irecovery || echo /opt/homebrew/bin/irecovery)"

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
  "$VENV_PY" "$ROOT_DIR/scripts/02_patch_engine.py" \
    --raw-dir "$ROOT_DIR/.cache/engine/raw" \
    --out-dir "$ENGINE_DIR" \
    --api-dir "$LOCAL_API_DIR" \
    --ipsw-1588 "$ipsw_1588"

  TELEMETRY_STEP="PREPARE_5_KERNEL_DT"
  echo ">>> [yigitzra1n 5/7] Building kernelcache v6 & DeviceTree v5 from stock IPSWs..."
  "$VENV_PY" "$ROOT_DIR/scripts/03_build_kernel_v6.py" \
    --ipsw-1588 "$ipsw_1588" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out "$BUILD_DIR/kernelcache.release.n71_v6.im4p"

  "$VENV_PY" "$ROOT_DIR/scripts/04_build_dtree_v5.py" \
    --ipsw-1588 "$ipsw_1588" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out "$BUILD_DIR/DeviceTree.n71map_v5.im4p" \
    --out-ramdisk "$BUILD_DIR/DeviceTree.n71map_ramdisk.im4p"

  TELEMETRY_STEP="PREPARE_6_RAMDISK"
  echo ">>> [yigitzra1n 6/7] Building Restore RamDisk (14-patch) & Maintenance SSH RamDisk..."
  "$VENV_PY" "$ROOT_DIR/scripts/05_build_ramdisk.py" \
    --ipsw-ipad5 "$ipsw_ipad5" \
    --out-restore-rd "$BUILD_DIR/098-68700-067.dmg" \
    --out-restore-tc "$BUILD_DIR/098-68700-067.dmg.trustcache" \
    --procursus-dir "$PROCURSUS_DIR" \
    --ssh-rd-init "$BUILD_DIR/bin/ssh_rd_init" \
    --out-ssh-rd "$BUILD_DIR/ssh_ramdisk.im4p" \
    --out-ssh-tc "$BUILD_DIR/ssh_ramdisk.trustcache"

  TELEMETRY_STEP="PREPARE_7_ROOTFS"
  echo ">>> [yigitzra1n 7/7] Building Hybrid iOS 16.7.16 RootFS & staging restore directory (requires sudo)..."
  IPSW_DIR="$IPSW_DIR" bash "$ROOT_DIR/scripts/06_build_rootfs.sh"

  telemetry_send "install_success" "prepare" "PREPARE_DONE" "yigitzra1n.sh" "$LINENO" 0 "prepared" ""
  echo ""
  echo "[+] yigitzra1n preparation complete! Everything is verified in $BUILD_DIR."
  echo "    Next step: Ensure your iPhone 6s is on iOS 15.8.8, then run:"
  echo "      ./yigitzra1n.sh install"
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
  local ipsw_1588
  ipsw_1588="$(find_ipsw "$IPSW_6S_1588_GLOB")"
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
        echo "[!] STEP 1/3 did not produce a boot block." >&2
        printf "    Retry STEP 1/3? [Y/n]: "
        read -r retry_ans
        if [[ "${retry_ans:-Y}" =~ ^[Nn]$ ]]; then
          telemetry_send "install_fail" "install" "STEP1_SHC" "yigitzra1n.sh" "$LINENO" 1 "shc_capture_failed" "SHC boot block capture failed"
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
      prompt_dfu "STEP 2/3 — Write Hybrid iOS 16.7.16 to NAND (DFU #2)"
      echo "[*] Entering pwnDFU..."
      if run_engine_clean ./bin/engine_helper -D; then
        echo "[*] Starting NAND restore (if exploit pauses >10s, briefly replug USB for 1 second)..."
        if DYLD_INSERT_LIBRARIES="$BUILD_DIR/bin/fast_dns.dylib" \
          run_engine_clean ./bin/engine_restore -y -o --allow-unsupport "$restore_blk_flag" "$boot_blk" "$BUILD_DIR/hybrid_ipsw"; then
          echo "[+] Hybrid iOS 16.7.16 NAND restore completed!"
          break
        fi
      fi
      echo "[!] STEP 2/3 (NAND restore) failed." >&2
      printf "    Retry STEP 2/3? [Y/n]: "
      read -r retry_ans
      if [[ "${retry_ans:-Y}" =~ ^[Nn]$ ]]; then
        telemetry_send "install_fail" "install" "STEP2_RESTORE" "yigitzra1n.sh" "$LINENO" 1 "nand_restore_failed" "Hybrid NAND restore failed"
        telemetry_mark_reported
        exit 1
      fi
    done

    save_install_state "STEP2_RESTORE_DONE"
  fi

  TELEMETRY_STEP="STEP3_BOOT"
  echo ""
  echo "[+] Hybrid iOS 16.7.16 is installed on NAND and your boot block is saved:"
  echo "    ${boot_blk:-$ENGINE_DIR/block/}"
  echo ""
  printf "STEP 3/3 — Boot into iOS 16.7.16 now? [Y/n]: "
  read -r boot_ans
  if [[ ! "${boot_ans:-Y}" =~ ^[Nn]$ ]]; then
    BLOCK_PATH="$boot_blk" cmd_boot
  fi
  telemetry_send "install_success" "install" "INSTALL_DONE" "yigitzra1n.sh" "$LINENO" 0 "installed" ""
}

cmd_boot() {
  TELEMETRY_STEP="BOOT"
  check_macos_host
  check_cli_tools
  ensure_python_venv
  [[ -d "$BUILD_DIR/hybrid_ipsw" && -f "$BUILD_DIR/bin/hw_init_probe.bin" ]] || {
    echo "[ERROR] Build outputs missing. Run './yigitzra1n.sh prepare' first." >&2
    exit 1
  }
  prompt_dfu "Tethered Boot — iOS 16.7.16 (DFU Mode)"
  local py_args=()
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ -n "$BOOT_ARGS" ]] && py_args+=(--boot-args "$BOOT_ARGS")
  [[ "$QUIET" -eq 1 ]] && py_args+=(--quiet)
  [[ "$FORCE" -eq 1 ]] && py_args+=(--force)
  "$VENV_PY" "$ROOT_DIR/scripts/pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}
  if [[ "$TELEMETRY_CMD" == "boot" ]]; then
    telemetry_send "install_success" "boot" "BOOT_DONE" "yigitzra1n.sh" "$LINENO" 0 "booted" ""
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
  prompt_dfu "Maintenance SSH RamDisk Boot (DFU Mode)"
  local py_args=(--ssh-ramdisk)
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ "$FORCE" -eq 1 ]] && py_args+=(--force)
  "$VENV_PY" "$ROOT_DIR/scripts/pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}

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
  read -r confirm
  if [[ "$confirm" != "YES" ]]; then
    echo "[*] Aborted."
    exit 0
  fi

  prompt_dfu "Uninstall / Reset APFS — Maintenance SSH RamDisk (DFU Mode)"
  local py_args=(--ssh-ramdisk)
  [[ -n "$BLOCK_PATH" ]] && py_args+=(--block "$BLOCK_PATH")
  [[ "$FORCE" -eq 1 ]] && py_args+=(--force)
  "$VENV_PY" "$ROOT_DIR/scripts/pongo_wifi_boot.py" ${py_args[@]+"${py_args[@]}"}

  local iproxy_pid=""
  local ssh_port="$DEVICE_PORT"
  cleanup_uninstall() {
    if [[ -n "$iproxy_pid" ]]; then
      kill "$iproxy_pid" 2>/dev/null || true
      wait "$iproxy_pid" 2>/dev/null || true
    fi
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
  trap - EXIT
  rm -f "$INSTALL_STATE_FILE"
  telemetry_send "install_success" "uninstall" "UNINSTALL_DONE" "yigitzra1n.sh" "$LINENO" 0 "uninstalled" ""
  sleep 5

  echo ""
  echo "[+] APFS container reset complete! Your iPhone 6s is now ready for stock iOS 15.8.8."
  local ipsw_1588=""
  ipsw_1588="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$IPSW_6S_1588_GLOB" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$ipsw_1588" && -f "$ipsw_1588" ]] && command -v idevicerestore >/dev/null 2>&1; then
    echo "    Detected local stock IPSW: $ipsw_1588"
    printf "    Start stock iOS 15.8.8 restore now via idevicerestore? [y/N]: "
    local rest_ans=""
    read -r rest_ans
    if [[ "$rest_ans" =~ ^[Yy]([Ee][Ss])?$ ]]; then
      mkdir -p "$BUILD_DIR/.tmp/restore_tmp" "$BUILD_DIR/.tmp/restore_cache"
      TMPDIR="$BUILD_DIR/.tmp/restore_tmp" idevicerestore -e -C "$BUILD_DIR/.tmp/restore_cache" "$ipsw_1588"
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

  DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$DEVICE_PORT" \
    bash "$ROOT_DIR/scripts/apply_device_patches.sh" ${aop_arg[@]+"${aop_arg[@]}"}

  if [[ -n "$IC_INFO" && -f "$IC_INFO" ]]; then
    echo "[*] Installing device FairPlay IC-Info.sisv..."
    sshpass -p "${SSH_PASS:-alpine}" ssh \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o PubkeyAuthentication=no -p "$DEVICE_PORT" "root@${DEVICE_HOST}" '
      set -e
      mkdir -p /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes
      cat > /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes/IC-Info.sisv
      chown -R mobile:mobile /private/var/mobile/Library/FairPlay
      chmod 755 /private/var/mobile/Library/FairPlay /private/var/mobile/Library/FairPlay/iTunes_Control /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes
      chmod 644 /private/var/mobile/Library/FairPlay/iTunes_Control/iTunes/IC-Info.sisv
      killall -9 fairplayd.A2 2>/dev/null || true
    ' < "$IC_INFO"
    echo "[+] IC-Info.sisv installed."
  fi
  telemetry_send "install_success" "patch-device" "PATCH_DEVICE_DONE" "yigitzra1n.sh" "$LINENO" 0 "patched" ""
}

cmd_activate() {
  TELEMETRY_STEP="ACTIVATE"
  DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$DEVICE_PORT" \
    bash "$ROOT_DIR/scripts/activate_device.sh"
}

cmd_clean() {
  echo "[*] Removing $BUILD_DIR (keeping ipsw/ and .cache/)..."
  rm -rf "$BUILD_DIR"
  echo "[+] Clean complete."
}

case "$CMD" in
  prepare)      cmd_prepare ;;
  install)      cmd_install ;;
  boot)         cmd_boot ;;
  ssh-ramdisk)  cmd_ssh_ramdisk ;;
  uninstall)    cmd_uninstall ;;
  activate)     cmd_activate ;;
  patch-device) cmd_patch_device ;;
  clean)        cmd_clean ;;
  *) echo "[ERROR] Unknown command: $CMD" >&2; usage; exit 2 ;;
esac
