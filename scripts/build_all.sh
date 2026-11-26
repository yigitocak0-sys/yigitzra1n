#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/scripts/telemetry.sh"
trap 'telemetry_trap_err "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_3_BUILD}" "build_all.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR
BUILD_DIR="$ROOT_DIR/build"
BIN_DIR="$BUILD_DIR/bin"
mkdir -p "$BUILD_DIR/.tmp" "$BIN_DIR"

CURRENT_STEP="build_init"

run_logged() {
  local line_no="$1"
  shift
  local logf="$BUILD_DIR/.tmp/build_step.log"
  local rc=0
  set +e
  "$@" >"$logf" 2>&1
  rc=$?
  set -e
  if [[ "$rc" -ne 0 && "$1" == "clang" ]]; then
    local can_classic=1
    local arg
    for arg in "$@"; do
      if [[ "$arg" == "-c" || "$arg" == "-Wl,-ld_classic" ]]; then
        can_classic=0
        break
      fi
    done
    if [[ "$can_classic" -eq 1 ]]; then
      local log_classic="$BUILD_DIR/.tmp/build_step_classic.log"
      local rc2=0
      set +e
      "$@" -Wl,-ld_classic >"$log_classic" 2>&1
      rc2=$?
      set -e
      if [[ "$rc2" -eq 0 ]]; then
        cat "$log_classic"
        rm -f "$logf" "$log_classic"
        return 0
      fi
      rm -f "$log_classic"
    fi
  fi
  if [[ "$rc" -ne 0 ]]; then
    cat "$logf" >&2 || true
    local err_tail=""
    err_tail="$(python3 - "$logf" <<'PY' 2>/dev/null || true
import pathlib, re, sys
lines = [l.strip() for l in pathlib.Path(sys.argv[1]).read_text(errors="ignore").splitlines() if l.strip()]
picked = [l for l in lines if re.search(r"(fatal error:|error:|ld:|Undefined symbols)", l)]
tail = picked[-3:] if picked else lines[-4:]
print(" | ".join(tail)[:210])
PY
)"
    telemetry_send "install_fail" "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_3_BUILD}" "build_all.sh" "$line_no" "$rc" "build_step_failed" "${CURRENT_STEP}: ${err_tail:-exit $rc}"
    telemetry_mark_reported
    exit "$rc"
  fi
  if [[ -s "$logf" ]]; then
    cat "$logf"
  fi
  return 0
}

CURRENT_STEP="1/11 fast_dns.dylib"
echo "[1/11] Building fast_dns.dylib (macOS local DNS interposer)..."
run_logged "$LINENO" clang -O2 -dynamiclib -Wall -Wextra \
  "$ROOT_DIR/src/fast_dns.c" \
  -o "$BIN_DIR/fast_dns.dylib"
codesign -f -s - "$BIN_DIR/fast_dns.dylib" 2>/dev/null || true

CURRENT_STEP="2/11 hw_init_probe.bin"
echo "[2/11] Building hw_init_probe.bin (PongoOS ARM64 kext bundle: Touch+WLAN+BT+CamLDO+SN2400+BootLogo)..."
run_logged "$LINENO" clang -target arm64-apple-darwin -Os -mkernel -ffreestanding -fno-stack-protector \
  -Wall -Wextra -Wl,-kext -Wl,-undefined,dynamic_lookup -nostdlib \
  "$ROOT_DIR/src/hw_init_probe.c" \
  -o "$BIN_DIR/hw_init_probe.bin"

IOS_SDK=""
if ! IOS_SDK="$(python3 "$ROOT_DIR/scripts/prepare_ios_sdk.py" "$ROOT_DIR")"; then
  echo "[!] No iPhoneOS SDK or macOS CommandLineTools SDK found. Run: xcode-select --install" >&2
  echo "    (hw_init_probe.bin and fast_dns.dylib were built in $BIN_DIR.)" >&2
  telemetry_send "install_fail" "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_3_BUILD}" "build_all.sh" "$LINENO" 1 "missing_iphoneos_sdk" "Neither iPhoneOS SDK nor CommandLineTools SDK found"
  telemetry_mark_reported
  exit 1
fi
echo "[*] Using iOS SDK: $IOS_SDK"

CLANG=(clang -isysroot "$IOS_SDK" -target arm64-apple-ios15.0 -O2 -Wall -Wextra)

CURRENT_STEP="3/11 sn2400_charger_daemon"
echo "[3/11] Building sn2400_charger_daemon (v6: boot clock, D2255 rails, AOP auto-load + mediaserverd refresh, 1A charge)..."
run_logged "$LINENO" "${CLANG[@]}" -framework CoreFoundation \
  "$ROOT_DIR/src/sn2400_charger_daemon.c" \
  -o "$BIN_DIR/sn2400_charger_daemon"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sn2400_charger_daemon.entitlements.plist" \
  "$BIN_DIR/sn2400_charger_daemon"

CURRENT_STEP="4/11 libn71m_avgestalt.dylib"
echo "[4/11] Building libn71m_avgestalt.dylib (Camera & Preferences N71m capability fix)..."
run_logged "$LINENO" "${CLANG[@]}" -dynamiclib -framework Foundation \
  -install_name /usr/lib/libn71m_avgestalt.dylib \
  "$ROOT_DIR/src/libn71m_avgestalt.m" \
  -o "$BIN_DIR/libn71m_avgestalt.dylib"
run_logged "$LINENO" codesign -f -s - "$BIN_DIR/libn71m_avgestalt.dylib"

CURRENT_STEP="5/11 libn71m_tipsmodel.dylib"
echo "[5/11] Building libn71m_tipsmodel.dylib (Tips.app / tipsd A1905 model hook)..."
run_logged "$LINENO" "${CLANG[@]}" -dynamiclib -framework Foundation \
  -install_name /usr/lib/libn71m_tipsmodel.dylib \
  "$ROOT_DIR/src/libn71m_tipsmodel.m" \
  -o "$BIN_DIR/libn71m_tipsmodel.dylib"
run_logged "$LINENO" codesign -f -s - "$BIN_DIR/libn71m_tipsmodel.dylib"

CURRENT_STEP="6/11 sleepd_stub"
echo "[6/11] Building sleepd_stub..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/sleepd_stub.c" -o "$BIN_DIR/sleepd_stub"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sleepd_stub.entitlements.plist" \
  "$BIN_DIR/sleepd_stub"

CURRENT_STEP="7/11 ios16_sshd_launcher"
echo "[7/11] Building ios16_sshd_launcher (native Mach-O OpenSSH bootstrapper, zero /bin/sh dependency)..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/ios16_sshd_launcher.c" -o "$BIN_DIR/ios16_sshd_launcher"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/ios16_sshd_launcher"

CURRENT_STEP="8/11 ssh_rd_init"
echo "[8/11] Building ssh_rd_init (native Mach-O Maintenance SSH RamDisk supervisor)..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/ssh_rd_init.c" -o "$BIN_DIR/ssh_rd_init"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/ssh_rd_init"

CURRENT_STEP="9/11 rd_reset_apfs"
echo "[9/11] Building rd_reset_apfs (native SSH RamDisk unmount + APFS container reset helper)..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/rd_reset_apfs.c" -o "$BIN_DIR/rd_reset_apfs"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/rd_reset_apfs"

CURRENT_STEP="10/11 ios16_skip_setup"
echo "[10/11] Building ios16_skip_setup (native SetupAssistant & purplebuddy bypass helper)..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/ios16_skip_setup.c" -o "$BIN_DIR/ios16_skip_setup"
run_logged "$LINENO" codesign -f -s - -i com.apple.purplebuddy --entitlements "$ROOT_DIR/entitlements/ios16_skip_setup.entitlements.plist" \
  "$BIN_DIR/ios16_skip_setup"

CURRENT_STEP="11/12 soft_aks.o"
echo "[11/12] Building soft_aks.o (SEP-less securityd software AKS helper, size-optimized)..."
run_logged "$LINENO" clang -isysroot "$IOS_SDK" -target arm64-apple-ios15.0 -Os -fno-vectorize -fno-slp-vectorize -fno-unroll-loops \
  -Wall -Wextra -c "$ROOT_DIR/src/soft_aks.c" -o "$BIN_DIR/soft_aks.o"

CURRENT_STEP="12/12 yigitzra1n_givemeroot"
echo "[12/12] Building yigitzra1n_givemeroot (Sileo root bridge for nosuid RootFS)..."
run_logged "$LINENO" "${CLANG[@]}" "$ROOT_DIR/src/yigitzra1n_givemeroot.c" -o "$BIN_DIR/yigitzra1n_givemeroot"
run_logged "$LINENO" codesign -f -s - --entitlements "$ROOT_DIR/entitlements/givemeroot.entitlements.plist" \
  "$BIN_DIR/yigitzra1n_givemeroot"

for f in sn2400_charger_daemon libn71m_avgestalt.dylib libn71m_tipsmodel.dylib sleepd_stub ios16_sshd_launcher ssh_rd_init rd_reset_apfs ios16_skip_setup soft_aks.o hw_init_probe.bin fast_dns.dylib yigitzra1n_givemeroot; do
  ln -sf "bin/$f" "$BUILD_DIR/$f"
done

echo ""
echo "[+] All binaries built and signed in $BIN_DIR:"
ls -lh "$BIN_DIR"
