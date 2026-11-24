#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build"
BIN_DIR="$BUILD_DIR/bin"
mkdir -p "$BUILD_DIR" "$BIN_DIR"

echo "[1/11] Building fast_dns.dylib (macOS local DNS interposer)..."
clang -O2 -dynamiclib -Wall -Wextra \
  "$ROOT_DIR/src/fast_dns.c" \
  -o "$BIN_DIR/fast_dns.dylib"
codesign -f -s - "$BIN_DIR/fast_dns.dylib" 2>/dev/null || true

echo "[2/11] Building hw_init_probe.bin (PongoOS ARM64 kext bundle: Touch+WLAN+BT+CamLDO+SN2400+BootLogo)..."
clang -target arm64-apple-darwin -Os -mkernel -ffreestanding -fno-stack-protector \
  -Wall -Wextra -Wl,-kext -Wl,-undefined,dynamic_lookup -nostdlib \
  "$ROOT_DIR/src/hw_init_probe.c" \
  -o "$BIN_DIR/hw_init_probe.bin"

find_iphoneos_sdk() {
  local sdk=""
  if sdk="$(xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)" && [[ -d "$sdk" ]]; then
    echo "$sdk"
    return 0
  fi
  for app in /Applications/Xcode*.app; do
    if [[ -d "$app/Contents/Developer" ]]; then
      if sdk="$(DEVELOPER_DIR="$app/Contents/Developer" xcrun --sdk iphoneos --show-sdk-path 2>/dev/null)" && [[ -d "$sdk" ]]; then
        echo "$sdk"
        return 0
      fi
    fi
  done
  return 1
}

IOS_SDK=""
if ! IOS_SDK="$(find_iphoneos_sdk)"; then
  echo "[!] iPhoneOS SDK not found. Install Xcode (or set DEVELOPER_DIR to Xcode.app/Contents/Developer)." >&2
  echo "    (hw_init_probe.bin and fast_dns.dylib were built in $BIN_DIR.)" >&2
  exit 1
fi
echo "[*] Using iPhoneOS SDK: $IOS_SDK"

CLANG=(clang -isysroot "$IOS_SDK" -target arm64-apple-ios15.0 -O2 -Wall -Wextra)

echo "[3/11] Building sn2400_charger_daemon (v6: boot clock, D2255 rails, AOP auto-load + mediaserverd refresh, 1A charge)..."
"${CLANG[@]}" -framework CoreFoundation \
  "$ROOT_DIR/src/sn2400_charger_daemon.c" \
  -o "$BIN_DIR/sn2400_charger_daemon"
codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sn2400_charger_daemon.entitlements.plist" \
  "$BIN_DIR/sn2400_charger_daemon"

echo "[4/11] Building libn71m_avgestalt.dylib (Camera & Preferences N71m capability fix)..."
"${CLANG[@]}" -dynamiclib -framework Foundation \
  -install_name /usr/lib/libn71m_avgestalt.dylib \
  "$ROOT_DIR/src/libn71m_avgestalt.m" \
  -o "$BIN_DIR/libn71m_avgestalt.dylib"
codesign -f -s - "$BIN_DIR/libn71m_avgestalt.dylib"

echo "[5/11] Building libn71m_tipsmodel.dylib (Tips.app / tipsd A1905 model hook)..."
"${CLANG[@]}" -dynamiclib -framework Foundation \
  -install_name /usr/lib/libn71m_tipsmodel.dylib \
  "$ROOT_DIR/src/libn71m_tipsmodel.m" \
  -o "$BIN_DIR/libn71m_tipsmodel.dylib"
codesign -f -s - "$BIN_DIR/libn71m_tipsmodel.dylib"

echo "[6/11] Building sleepd_stub..."
"${CLANG[@]}" "$ROOT_DIR/src/sleepd_stub.c" -o "$BIN_DIR/sleepd_stub"
codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sleepd_stub.entitlements.plist" \
  "$BIN_DIR/sleepd_stub"

echo "[7/11] Building ios16_sshd_launcher (native Mach-O OpenSSH bootstrapper, zero /bin/sh dependency)..."
"${CLANG[@]}" "$ROOT_DIR/src/ios16_sshd_launcher.c" -o "$BIN_DIR/ios16_sshd_launcher"
codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/ios16_sshd_launcher"

echo "[8/11] Building ssh_rd_init (native Mach-O Maintenance SSH RamDisk supervisor)..."
"${CLANG[@]}" "$ROOT_DIR/src/ssh_rd_init.c" -o "$BIN_DIR/ssh_rd_init"
codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/ssh_rd_init"

echo "[9/11] Building rd_reset_apfs (native SSH RamDisk unmount + APFS container reset helper)..."
"${CLANG[@]}" "$ROOT_DIR/src/rd_reset_apfs.c" -o "$BIN_DIR/rd_reset_apfs"
codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" \
  "$BIN_DIR/rd_reset_apfs"

echo "[10/11] Building ios16_skip_setup (native SetupAssistant & purplebuddy bypass helper)..."
"${CLANG[@]}" "$ROOT_DIR/src/ios16_skip_setup.c" -o "$BIN_DIR/ios16_skip_setup"
codesign -f -s - -i com.apple.purplebuddy --entitlements "$ROOT_DIR/entitlements/ios16_skip_setup.entitlements.plist" \
  "$BIN_DIR/ios16_skip_setup"

echo "[11/11] Building soft_aks.o (SEP-less securityd software AKS helper, size-optimized)..."
clang -isysroot "$IOS_SDK" -target arm64-apple-ios15.0 -Os -fno-vectorize -fno-slp-vectorize -fno-unroll-loops \
  -Wall -Wextra -c "$ROOT_DIR/src/soft_aks.c" -o "$BIN_DIR/soft_aks.o"

for f in sn2400_charger_daemon libn71m_avgestalt.dylib libn71m_tipsmodel.dylib sleepd_stub ios16_sshd_launcher ssh_rd_init rd_reset_apfs ios16_skip_setup soft_aks.o hw_init_probe.bin fast_dns.dylib; do
  ln -sf "bin/$f" "$BUILD_DIR/$f"
done

echo ""
echo "[+] All binaries built and signed in $BIN_DIR:"
ls -lh "$BIN_DIR"
