#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -f "$SCRIPT_DIR/patch_mobileactivationd.py" ]]; then
  ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  ROOT_DIR="$SCRIPT_DIR"
fi

source "$ROOT_DIR/scripts/telemetry.sh"
ACTIVATE_STEP="INIT"
export TELEMETRY_MARKER_FILE="${TELEMETRY_MARKER_FILE:-$ROOT_DIR/build/.tmp/.telemetry_reported}"

BUILD_DIR="$ROOT_DIR/build"
BIN_DIR="$BUILD_DIR/bin"
ENGINE_DIR="$BUILD_DIR/engine"
DEVICE_HOST="${DEVICE_HOST:-127.0.0.1}"
DEVICE_PORT="${DEVICE_PORT:-2222}"
SSH_PASS="${SSH_PASS:-alpine}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) DEVICE_HOST="$2"; shift 2 ;;
    --port) DEVICE_PORT="$2"; shift 2 ;;
    --pass) SSH_PASS="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--host 127.0.0.1] [--port 2222] [--pass alpine]"
      exit 0
      ;;
    *) echo "[ERROR] Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ "${TELEMETRY_PARENT:-0}" == "1" ]] || rm -f "$TELEMETRY_MARKER_FILE" 2>/dev/null || true
trap 'telemetry_trap_err "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR

mkdir -p "$BUILD_DIR/.tmp" "$BIN_DIR"
WORK_TMP="$(mktemp -d "$BUILD_DIR/.tmp/activate_n71m.XXXXXX")"
IPROXY_PID=""
cleanup() {
  if [[ -n "$IPROXY_PID" ]]; then
    kill "$IPROXY_PID" 2>/dev/null || true
    wait "$IPROXY_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_TMP"
}
trap cleanup EXIT

ensure_skip_setup_binary() {
  local bin_out="$BIN_DIR/ios16_skip_setup"
  local src_c="$ROOT_DIR/src/ios16_skip_setup.c"
  local ent_plist="$ROOT_DIR/entitlements/ios16_skip_setup.entitlements.plist"

  if [[ -f "$bin_out" && "$bin_out" -nt "$src_c" && "$bin_out" -nt "$ent_plist" ]]; then
    return 0
  fi

  local sdk=""
  if ! sdk="$(python3 "$ROOT_DIR/scripts/prepare_ios_sdk.py" "$ROOT_DIR")"; then
    echo "[ERROR] No iPhoneOS SDK or macOS CommandLineTools SDK found. Run: xcode-select --install" >&2
    exit 1
  fi

  clang -target arm64-apple-ios15.0 -isysroot "$sdk" -O2 -Wall -Wextra \
    -Wl,-adhoc_codesign "$src_c" -o "$bin_out"
  codesign -f -s - -i com.apple.purplebuddy --entitlements "$ent_plist" "$bin_out"
  ln -sf "bin/ios16_skip_setup" "$BUILD_DIR/ios16_skip_setup"
}

ACTIVATE_STEP="BUILD_SKIP_SETUP"
ensure_skip_setup_binary

ACTIVATE_STEP="VERIFY_USB_DEVICE"
DEV_ECID=""
if command -v ideviceinfo >/dev/null 2>&1; then
  USB_PROD="$(ideviceinfo -k ProductType 2>/dev/null | tr -d '\r\n' || true)"
  USB_VER="$(ideviceinfo -k ProductVersion 2>/dev/null | tr -d '\r\n' || true)"
  DEV_ECID="$(ideviceinfo -k UniqueChipID 2>/dev/null | tr -d '\r\n' || true)"
  if [[ -n "$USB_PROD" && "$USB_PROD" != "iPhone8,1" ]]; then
    echo "[ERROR] Unsupported device model: $USB_PROD (only iPhone 6s is supported)." >&2
    telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 77 "unsupported_model" "Product=$USB_PROD"
    telemetry_mark_reported
    exit 77
  fi
  if [[ -n "$USB_VER" && "$USB_VER" != 16.* ]]; then
    echo "[ERROR] Target device is not running iOS 16 ($USB_VER)." >&2
    telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 77 "non_ios16" "Version=$USB_VER"
    telemetry_mark_reported
    exit 77
  fi
fi

if [[ "$DEVICE_HOST" == "127.0.0.1" || "$DEVICE_HOST" == "localhost" ]]; then
  if ! lsof -iTCP:"$DEVICE_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    if command -v iproxy >/dev/null 2>&1; then
      if iproxy --help 2>&1 | grep -q "LOCAL_PORT:DEVICE_PORT"; then
        iproxy "${DEVICE_PORT}:22" >/dev/null 2>&1 &
      else
        iproxy "$DEVICE_PORT" 22 >/dev/null 2>&1 &
      fi
      IPROXY_PID=$!
      sleep 1
    fi
  fi
fi

ASKPASS_SCRIPT="$WORK_TMP/askpass.sh"
cat > "$ASKPASS_SCRIPT" <<EOF
#!/bin/sh
echo "$SSH_PASS"
EOF
chmod 700 "$ASKPASS_SCRIPT"

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o PubkeyAuthentication=no
  -o PreferredAuthentications=password
  -o ConnectTimeout=10
  -p "$DEVICE_PORT"
)

run_ssh() {
  SSH_ASKPASS="$ASKPASS_SCRIPT" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
    ssh "${SSH_OPTS[@]}" "root@${DEVICE_HOST}" "$@"
}

ACTIVATE_STEP="WAIT_SSH"
echo "[1/4] Verifying device identity & upgrade proof..."
ssh_ready=0
for _try in {1..15}; do
  if run_ssh "true" >/dev/null 2>&1; then
    ssh_ready=1
    break
  fi
  sleep 2
done
if [[ "$ssh_ready" -ne 1 ]]; then
  echo "[ERROR] Could not connect to device SSH on ${DEVICE_HOST}:${DEVICE_PORT}." >&2
  telemetry_send "install_fail" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 1 "ssh_connect_failed" "SSH timeout on port ${DEVICE_PORT}"
  telemetry_mark_reported
  exit 1
fi
python3 - "$ENGINE_DIR" "$DEV_ECID" "$WORK_TMP/receipt.plist" <<'PY'
import plistlib, sys
from pathlib import Path

ecid = sys.argv[2].strip()
out_plist = Path(sys.argv[3])

tok = bytes(x ^ 0x5A for x in [
    0x03,0x13,0x1d,0x13,0x0e,0x00,0x08,0x1b,0x6b,0x14,0x77,0x14,0x6d,0x6b,0x17,0x77,
    0x13,0x15,0x09,0x6b,0x6c,0x77,0x16,0x1f,0x1d,0x13,0x0e,0x77,0x0f,0x0a,0x1d,0x08,
    0x1b,0x1e,0x1f,0x77,0x0c,0x6b
]).decode("ascii")

with open(out_plist, "wb") as f:
    plistlib.dump({
        "Tool": "yigitzra1n",
        "TargetProductType": "iPhone8,1",
        "TargetHardwareModel": "N71mAP",
        "UpgradedProductVersion": "16.7.16",
        "UpgradedBuildVersion": "20H392",
        "DeviceECID": ecid or "local",
        "ReceiptMagic": tok,
    }, f, fmt=plistlib.FMT_XML)
PY

GATE_TOKEN="$(python3 - "$WORK_TMP/receipt.plist" <<'PY'
import plistlib, sys
with open(sys.argv[1], "rb") as f:
    print(plistlib.load(f)["ReceiptMagic"])
PY
)"

REC_REMOTE_PATH="$(python3 - <<'PY'
print("/" + bytes(x ^ 0x5A for x in [
    0x09,0x23,0x29,0x2e,0x3f,0x37,0x75,0x16,0x33,0x38,0x28,0x3b,0x28,0x23,0x75,
    0x19,0x35,0x28,0x3f,0x09,0x3f,0x28,0x2c,0x33,0x39,0x3f,0x29,0x75,0x74,0x23,0x33,
    0x3d,0x33,0x2e,0x20,0x28,0x3b,0x6b,0x34,0x05,0x2f,0x2a,0x3d,0x28,0x3b,0x3e,0x3f,
    0x05,0x28,0x3f,0x39,0x3f,0x33,0x2a,0x2e,0x74,0x2a,0x36,0x33,0x29,0x2e
]).decode("ascii"))
PY
)"

ACTIVATE_STEP="VERIFY_ENV_AND_RECEIPT"
rc=0
run_ssh "
  set -e
  export PATH=\"/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\"
  if [ ! -f /usr/local/libexec/sn2400_charger_daemon ] || [ ! -f /usr/local/libexec/ios16_sshd_launcher ]; then
    echo '[ERROR] Target environment validation failed (78).' >&2
    exit 78
  fi
  /sbin/mount -u -w / 2>/dev/null || /sbin/mount -uw / 2>/dev/null || mount -uw / 2>/dev/null || true
  cat > '$REC_REMOTE_PATH'
  chmod 644 '$REC_REMOTE_PATH'
" < "$WORK_TMP/receipt.plist" || rc=$?
if [[ "$rc" -ne 0 ]]; then
  if [[ "$rc" -eq 78 ]]; then
    telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 78 "missing_yigitzra1n_daemons_78" "Target environment validation failed (78)"
  else
    telemetry_send "install_fail" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$rc" "receipt_write_failed" "Failed writing receipt over SSH"
  fi
  telemetry_mark_reported
  exit "$rc"
fi

ACTIVATE_STEP="CHECK_SKIP_SETUP"
rc=0
run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -u -w / 2>/dev/null || /sbin/mount -uw / 2>/dev/null || mount -uw / 2>/dev/null || true
  mkdir -p /usr/local/bin
  cat > /usr/local/bin/ios16_skip_setup.new
  chmod 755 /usr/local/bin/ios16_skip_setup.new
  chown root:wheel /usr/local/bin/ios16_skip_setup.new 2>/dev/null || true
  mv -f /usr/local/bin/ios16_skip_setup.new /usr/local/bin/ios16_skip_setup
  /usr/local/bin/ios16_skip_setup --check
' < "$BIN_DIR/ios16_skip_setup" || rc=$?
if [[ "$rc" -ne 0 ]]; then
  case "$rc" in
    77) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 77 "target_hw_os_mismatch_77" "ios16_skip_setup check returned 77" ;;
    78) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 78 "receipt_or_daemon_invalid_78" "ios16_skip_setup check returned 78" ;;
    79) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 79 "fmi_lock_active_79" "Find My / Activation Lock detected (79)" ;;
    *)  telemetry_send "install_fail" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$rc" "skip_setup_check_failed" "ios16_skip_setup check returned $rc" ;;
  esac
  telemetry_mark_reported
  exit "$rc"
fi

ACTIVATE_STEP="PATCH_MOBILEACTIVATIOND"
echo "[2/4] Preparing components..."
run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -u -w / 2>/dev/null || /sbin/mount -uw / 2>/dev/null || mount -uw / 2>/dev/null || true
  mkdir -p /usr/local/share/backups
  if [ ! -f /usr/local/share/backups/mobileactivationd.orig ]; then
    if [ -f /usr/libexec/mobileactivationd.orig ]; then
      cp -p /usr/libexec/mobileactivationd.orig /usr/local/share/backups/mobileactivationd.orig
    else
      cp -p /usr/libexec/mobileactivationd /usr/local/share/backups/mobileactivationd.orig
    fi
  fi
  cat /usr/local/share/backups/mobileactivationd.orig
' > "$WORK_TMP/mobileactivationd.orig"

rc=0
patch_err="$(python3 "$ROOT_DIR/scripts/patch_mobileactivationd.py" \
  --in "$WORK_TMP/mobileactivationd.orig" \
  --out "$WORK_TMP/mobileactivationd.patched" \
  --gate "$GATE_TOKEN" 2>&1)" || rc=$?
printf '%s\n' "$patch_err"
if [[ "$rc" -ne 0 ]]; then
  if [[ "$patch_err" == *"code 78"* || "$patch_err" == *"Unsupported target binary"* ]]; then
    telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$rc" "mobileactivationd_gate_or_build" "$patch_err"
  else
    telemetry_send "install_fail" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$rc" "patch_mobileactivationd_failed" "$patch_err"
  fi
  telemetry_mark_reported
  exit "$rc"
fi

ACTIVATE_STEP="UPLOAD_MOBILEACTIVATIOND"
echo "[3/4] Installing components to device..."
run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -u -w / 2>/dev/null || /sbin/mount -uw / 2>/dev/null || mount -uw / 2>/dev/null || true
  cat > /usr/libexec/mobileactivationd.new
  chmod 755 /usr/libexec/mobileactivationd.new
  chown root:wheel /usr/libexec/mobileactivationd.new 2>/dev/null || true
  mv -f /usr/libexec/mobileactivationd.new /usr/libexec/mobileactivationd
' < "$WORK_TMP/mobileactivationd.patched"

ACTIVATE_STEP="RUN_SKIP_SETUP"
echo "[4/4] Finalizing setup & restarting SpringBoard..."
(
  set +e
  sb_file="$WORK_TMP/statusBarOverrides"
  if python3 "$ROOT_DIR/scripts/build_statusbar_overrides.py" --out "$sb_file" 2>/dev/null && [[ -s "$sb_file" ]]; then
    run_ssh '
      set -e
      mkdir -p /private/var/mobile/Library/SpringBoard
      chown mobile:mobile /private/var/mobile/Library /private/var/mobile/Library/SpringBoard 2>/dev/null || true
      cat > /private/var/mobile/Library/SpringBoard/statusBarOverrides.tmp
      chown mobile:mobile /private/var/mobile/Library/SpringBoard/statusBarOverrides.tmp 2>/dev/null || chown 501:501 /private/var/mobile/Library/SpringBoard/statusBarOverrides.tmp 2>/dev/null || true
      chmod 644 /private/var/mobile/Library/SpringBoard/statusBarOverrides.tmp
      mv -f /private/var/mobile/Library/SpringBoard/statusBarOverrides.tmp /private/var/mobile/Library/SpringBoard/statusBarOverrides
    ' < "$sb_file" 2>/dev/null || echo "[!] Non-fatal status bar override step skipped." >&2
  fi
) || true

rc=0
run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  killall -9 mobileactivationd 2>/dev/null || {
    for pid in $(ps -A -o pid,comm | sed -n "/mobileactivationd/s/^[ ]*\([0-9]*\).*/\1/p"); do
      kill -9 "$pid" 2>/dev/null || true
    done
  }
  sleep 1
  killall -9 lockdownd 2>/dev/null || {
    for pid in $(ps -A -o pid,comm | sed -n "/lockdownd/s/^[ ]*\([0-9]*\).*/\1/p"); do
      kill -9 "$pid" 2>/dev/null || true
    done
  }
  sleep 1
  /usr/local/bin/ios16_skip_setup
' || rc=$?
if [[ "$rc" -ne 0 ]]; then
  case "$rc" in
    77) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 77 "target_hw_os_mismatch_77" "ios16_skip_setup returned 77" ;;
    78) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 78 "receipt_or_daemon_invalid_78" "ios16_skip_setup returned 78" ;;
    79) telemetry_send "security_block" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" 79 "fmi_lock_active_79" "Find My / Activation Lock detected (79)" ;;
    *)  telemetry_send "install_fail" "${TELEMETRY_CMD:-activate}" "$ACTIVATE_STEP" "activate_device.sh" "$LINENO" "$rc" "finalize_setup_failed" "Setup finalization returned $rc" ;;
  esac
  telemetry_mark_reported
  exit "$rc"
fi

rc_ui=0
run_ssh '
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  killall -9 budd Setup SpringBoard 2>/dev/null || {
    for pid in $(ps -A -o pid,comm | sed -n "/budd/s/^[ ]*\([0-9]*\).*/\1/p;/\/Applications\/Setup\.app\/Setup/s/^[ ]*\([0-9]*\).*/\1/p;/SpringBoard\.app\/SpringBoard/s/^[ ]*\([0-9]*\).*/\1/p"); do
      kill -9 "$pid" 2>/dev/null || true
    done
  }
  sync
' || rc_ui=$?
if [[ "$rc_ui" -ne 0 && "$rc_ui" -ne 255 ]]; then
  echo "[!] Non-fatal UI restart exit code ($rc_ui); continuing..." >&2
fi

if command -v ssh-keygen >/dev/null 2>&1; then
  ssh-keygen -R "[${DEVICE_HOST}]:${DEVICE_PORT}" >/dev/null 2>&1 || true
fi

sleep 3
if command -v ideviceinfo >/dev/null 2>&1; then
  ideviceinfo | grep -E "ActivationState|BrickState" || true
fi
telemetry_send "install_success" "${TELEMETRY_CMD:-activate}" "ACTIVATE_DONE" "activate_device.sh" "$LINENO" 0 "activated" ""
telemetry_mark_reported
echo "[+] Done! Your iPhone 6s is activated and ready."
echo ""
echo "======================================================================"
echo "  Share your feedback or leave a message for me (I read all of them!)"
echo "======================================================================"
if [[ -t 0 ]]; then
  echo "  Type a short comment/thanks below and press Enter to send it now,"
  echo "  or just press Enter to skip."
  echo "  (You can also send anytime:  ./yigitzra1n.sh message"
  echo "   and check for my reply:    ./yigitzra1n.sh messages)"
  printf "  > "
  user_feedback=""
  IFS= read -r user_feedback || true
  if [[ -n "${user_feedback//[[:space:]]/}" ]]; then
    telemetry_send_message "$user_feedback" "activate" || true
  fi
else
  echo "  Send a message / feedback : ./yigitzra1n.sh message"
  echo "  Check for my reply        : ./yigitzra1n.sh messages"
fi
