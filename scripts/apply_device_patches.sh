#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/telemetry.sh"
trap 'telemetry_trap_err "${TELEMETRY_CMD:-patch-device}" "${TELEMETRY_STEP:-PATCH_DEVICE}" "apply_device_patches.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR
BUILD_DIR="$ROOT_DIR/build"
BIN_DIR="$BUILD_DIR/bin"
DEVICE_HOST="${DEVICE_HOST:-127.0.0.1}"
DEVICE_PORT="${DEVICE_PORT:-2222}"
SSH_PASS="${SSH_PASS:-alpine}"
AOP_BIN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --aop-bin) AOP_BIN="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

for req in sn2400_charger_daemon libn71m_avgestalt.dylib libn71m_tipsmodel.dylib; do
  if [[ ! -f "$BIN_DIR/$req" ]]; then
    echo "Missing $BIN_DIR/$req. Run ./scripts/build_all.sh first." >&2
    exit 1
  fi
done

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o PreferredAuthentications=password -o ConnectTimeout=5 -p "$DEVICE_PORT")
run_ssh() { sshpass -p "$SSH_PASS" ssh "${SSH_OPTS[@]}" "root@${DEVICE_HOST}" "$@"; }

mkdir -p "$BUILD_DIR/.tmp"
WORK_TMP="$(mktemp -d "$BUILD_DIR/.tmp/n71m_apply.XXXXXX")"
IPROXY_PID=""
cleanup() {
  if [[ -n "$IPROXY_PID" ]]; then
    kill "$IPROXY_PID" 2>/dev/null || true
    wait "$IPROXY_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_TMP"
}
trap cleanup EXIT

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

ssh_ready=0
for _try in {1..10}; do
  if run_ssh "true" >/dev/null 2>&1; then
    ssh_ready=1
    break
  fi
  sleep 1
done
if [[ "$ssh_ready" -ne 1 ]]; then
  echo "[ERROR] Could not connect to device SSH on ${DEVICE_HOST}:${DEVICE_PORT}." >&2
  telemetry_send "install_fail" "${TELEMETRY_CMD:-patch-device}" "WAIT_SSH" "apply_device_patches.sh" "$LINENO" 1 "ssh_connect_failed" "SSH timeout on port ${DEVICE_PORT}"
  telemetry_mark_reported
  exit 1
fi

echo "[1/5] Deploying sn2400_charger_daemon v6, dylibs, and optional AOP firmware..."
STAGE="$WORK_TMP/stage"
mkdir -p "$STAGE"
cp -p "$BIN_DIR/sn2400_charger_daemon" "$BIN_DIR/libn71m_avgestalt.dylib" "$BIN_DIR/libn71m_tipsmodel.dylib" "$STAGE/"
[[ -f "$BIN_DIR/ios16_sshd_launcher" ]] && cp -p "$BIN_DIR/ios16_sshd_launcher" "$STAGE/"
if [[ -n "$AOP_BIN" && -f "$AOP_BIN" ]]; then
  cp -p "$AOP_BIN" "$STAGE/s8000aop.bin"
fi

tar -C "$STAGE" -cf - . | run_ssh '
set -e
export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
/sbin/mount -uw / 2>/dev/null || true
mkdir -p /var/tmp/n71m_stage /usr/local/libexec /usr/local/lib /usr/local/share/firmware /usr/local/share/backups
tar -C /var/tmp/n71m_stage -xf -

for pair in \
  "sn2400_charger_daemon:/usr/local/libexec/sn2400_charger_daemon" \
  "libn71m_avgestalt.dylib:/usr/lib/libn71m_avgestalt.dylib" \
  "libn71m_tipsmodel.dylib:/usr/lib/libn71m_tipsmodel.dylib"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  rm -f "$dst"
  mv "/var/tmp/n71m_stage/$src" "$dst"
  chown root:wheel "$dst"
  chmod 755 "$dst"
done

if [ -f /var/tmp/n71m_stage/ios16_sshd_launcher ]; then
  rm -f /usr/local/libexec/ios16_sshd_launcher
  mv /var/tmp/n71m_stage/ios16_sshd_launcher /usr/local/libexec/ios16_sshd_launcher
  chown root:wheel /usr/local/libexec/ios16_sshd_launcher
  chmod 755 /usr/local/libexec/ios16_sshd_launcher
fi

if [ -f /var/tmp/n71m_stage/s8000aop.bin ]; then
  mv /var/tmp/n71m_stage/s8000aop.bin /usr/local/share/firmware/s8000aop.bin
  chown root:wheel /usr/local/share/firmware/s8000aop.bin
  chmod 644 /usr/local/share/firmware/s8000aop.bin
fi
rm -rf /var/tmp/n71m_stage
'

patch_remote_macho() {
  local remote_path="$1"
  local backup_name="$2"
  local dylib_install_name="$3"
  local bundle_id="$4"
  echo "  -> Patching $remote_path with $dylib_install_name..."
  sleep 1
  run_ssh "
    set -e
    export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    /sbin/mount -uw / 2>/dev/null || true
    mkdir -p /usr/local/share/backups
    [ -f /usr/local/share/backups/$backup_name ] || cp -p '$remote_path' '/usr/local/share/backups/$backup_name'
    cat '/usr/local/share/backups/$backup_name'
  " > "$WORK_TMP/$backup_name"

  codesign -d --entitlements - --xml "$WORK_TMP/$backup_name" 2>/dev/null > "$WORK_TMP/$backup_name.ent.plist" || true
  python3 "$ROOT_DIR/scripts/inject_dylib.py" "$WORK_TMP/$backup_name" "$dylib_install_name" "$WORK_TMP/$backup_name.patched"
  if [[ -s "$WORK_TMP/$backup_name.ent.plist" ]]; then
    codesign -f -s - -i "$bundle_id" --entitlements "$WORK_TMP/$backup_name.ent.plist" "$WORK_TMP/$backup_name.patched"
  else
    codesign -f -s - -i "$bundle_id" "$WORK_TMP/$backup_name.patched"
  fi

  sleep 1
  run_ssh "
    set -e
    export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    /sbin/mount -uw / 2>/dev/null || true
    cat > /var/tmp/patched_bin.new
    rm -f '$remote_path'
    mv /var/tmp/patched_bin.new '$remote_path'
    chown root:wheel '$remote_path'
    chmod 755 '$remote_path'
  " < "$WORK_TMP/$backup_name.patched"
}

echo "[2/5] Patching Camera.app & Preferences.app with libn71m_avgestalt.dylib..."
patch_remote_macho "/Applications/Camera.app/Camera" "Camera.orig" "/usr/lib/libn71m_avgestalt.dylib" "com.apple.camera"
patch_remote_macho "/Applications/Preferences.app/Preferences" "Preferences.orig" "/usr/lib/libn71m_avgestalt.dylib" "com.apple.Preferences"

echo "[3/5] Patching tipsd, Tips.app & locationd..."
patch_remote_macho "/usr/libexec/tipsd" "tipsd.orig" "/usr/lib/libn71m_tipsmodel.dylib" "com.apple.tipsd"
sleep 1
TIPS_PATH="$(run_ssh 'ls /var/containers/Bundle/Application/*/Tips.app/Tips 2>/dev/null | head -n 1 || true' | tr -d '\r')"
if [[ -n "$TIPS_PATH" ]]; then
  patch_remote_macho "$TIPS_PATH" "Tips.orig" "/usr/lib/libn71m_tipsmodel.dylib" "com.apple.tips"
fi

run_ssh '
  set -e
  export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -uw / 2>/dev/null || true
  [ -f /usr/local/share/backups/locationd.orig ] || cp -p /usr/libexec/locationd /usr/local/share/backups/locationd.orig
  cat /usr/local/share/backups/locationd.orig
' > "$WORK_TMP/locationd.orig"
python3 "$ROOT_DIR/scripts/patch_locationd.py" --in "$WORK_TMP/locationd.orig" --out "$WORK_TMP/locationd.patched"
run_ssh '
  set -e
  export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -uw / 2>/dev/null || true
  cat > /var/tmp/locationd.new
  rm -f /usr/libexec/locationd
  mv /var/tmp/locationd.new /usr/libexec/locationd
  chown root:wheel /usr/libexec/locationd
  chmod 755 /usr/libexec/locationd
' < "$WORK_TMP/locationd.patched"

echo "[4/5] Verifying SSH listener (:22) in launchd.plist & Procursus symlinks..."
sleep 1
run_ssh '
  set -e
  export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -uw / 2>/dev/null || true
  [ -f /usr/local/share/backups/launchd.plist.orig ] || cp -p /System/Library/xpc/launchd.plist /usr/local/share/backups/launchd.plist.orig
  cat /System/Library/xpc/launchd.plist
' > "$WORK_TMP/launchd.plist"

python3 - "$WORK_TMP/launchd.plist" "$WORK_TMP/launchd.plist.new" <<'PY'
import plistlib, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, 'rb') as f:
    d = plistlib.load(f)
for k, v in d.get('LaunchDaemons', {}).items():
    if 'dropbear' in k.lower() and isinstance(v, dict):
        args = v.get('ProgramArguments', [])
        v['ProgramArguments'] = ['22' if a == '127.0.0.1:22' else a for a in args]
with open(dst, 'wb') as f:
    plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
PY

sleep 1
run_ssh '
  set -e
  export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -uw / 2>/dev/null || true
  cat > /var/tmp/launchd.plist.new
  rm -f /System/Library/xpc/launchd.plist
  mv /var/tmp/launchd.plist.new /System/Library/xpc/launchd.plist
  chown root:wheel /System/Library/xpc/launchd.plist
  chmod 644 /System/Library/xpc/launchd.plist

  if [ -d /var/jb/usr/bin ]; then
    ln -sf /var/jb/usr/bin/bash /bin/sh 2>/dev/null || true
    ln -sf /var/jb/usr/bin/bash /bin/bash 2>/dev/null || true
    for tool in env apt apt-get apt-cache dpkg neofetch plutil oslog; do
      [ -x "/var/jb/usr/bin/$tool" ] && ln -sf "/var/jb/usr/bin/$tool" "/usr/bin/$tool" || true
    done
    [ -d /var/jb/etc/apt ] && ln -sfn /var/jb/etc/apt /etc/apt || true
  fi
' < "$WORK_TMP/launchd.plist.new"

echo "[5/5] Restarting sn2400_charger_daemon, locationd & tipsd..."
sleep 1
run_ssh '
  export PATH="/var/jb/usr/sbin:/var/jb/usr/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  killall sn2400_charger_daemon 2>/dev/null || true
  killall locationd 2>/dev/null || true
  killall tipsd 2>/dev/null || true
  sleep 2
  ps aux | grep -E "sn2400_charger_daemon|sshd|dropbear" | grep -v grep || true
'
echo "[+] Applying local activation & Setup.app bypass..."
DEVICE_HOST="$DEVICE_HOST" DEVICE_PORT="$DEVICE_PORT" SSH_PASS="$SSH_PASS" \
  bash "$ROOT_DIR/scripts/activate_device.sh"
echo "[+] All device patches applied cleanly!"
