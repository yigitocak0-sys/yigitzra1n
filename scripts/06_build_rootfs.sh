#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[*] Re-running 06_build_rootfs.sh with sudo to preserve RootFS ownership..."
  exec sudo -E bash "$0" "$@"
fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/config/firmware_urls.env"
source "$ROOT_DIR/scripts/telemetry.sh"
trap 'telemetry_trap_err "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_7_ROOTFS}" "06_build_rootfs.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR

IPSW_DIR="${IPSW_DIR:-$ROOT_DIR/ipsw}"
BUILD_DIR="$ROOT_DIR/build"
HYBRID_DIR="$BUILD_DIR/hybrid_ipsw"
SSH_RD_IPSW_DIR="$BUILD_DIR/ssh_ramdisk_ipsw"
BIN_DIR="$BUILD_DIR/bin"
PROCURSUS_DIR="$ROOT_DIR/.cache/procursus"
VENV_PY="$ROOT_DIR/.venv/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="python3"

find_ipsw() {
  local pat="$1"
  local f
  f="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$pat" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  [[ -n "$f" ]] || { echo "[ERROR] Missing IPSW matching $pat in $IPSW_DIR" >&2; exit 1; }
  echo "$f"
}

IPSW_1588="$(find_ipsw "$IPSW_6S_1588_GLOB")"
IPSW_IPAD5="$(find_ipsw "$IPSW_IPAD5_16716_GLOB")"
IPSW_IP8="$(find_ipsw "$IPSW_IP8_16716_GLOB")"

for req in \
  "$BUILD_DIR/kernelcache.release.n71_v6.im4p" \
  "$BUILD_DIR/DeviceTree.n71map_v5.im4p" \
  "$BUILD_DIR/098-68700-067.dmg" \
  "$BUILD_DIR/098-68700-067.dmg.trustcache" \
  "$BIN_DIR/sn2400_charger_daemon" \
  "$BIN_DIR/libn71m_avgestalt.dylib" \
  "$BIN_DIR/libn71m_tipsmodel.dylib" \
  "$BIN_DIR/sleepd_stub" \
  "$BIN_DIR/ios16_sshd_launcher" \
  "$BIN_DIR/soft_aks.o"; do
  [[ -f "$req" ]] || { echo "[ERROR] Missing build prerequisite: $req" >&2; exit 1; }
done

mkdir -p "$BUILD_DIR/.tmp"
TMP_AVAIL_GB="$(df -k /tmp | awk 'NR==2 {print int($4/1024/1024)}')"
if (( TMP_AVAIL_GB >= 20 )); then
  STAGE_ROOT="/tmp"
  echo "[*] Using fast /tmp staging (${TMP_AVAIL_GB} GB free on internal SSD)"
else
  STAGE_ROOT="$BUILD_DIR/.tmp"
  echo "[*] Using repo-volume staging at $STAGE_ROOT (/tmp has only ${TMP_AVAIL_GB} GB free)"
fi
export TMPDIR="$STAGE_ROOT"
STAGE_AVAIL_KB="$(df -k "$STAGE_ROOT" | awk 'NR==2 {print $4}')"
STAGE_AVAIL_GB=$(( STAGE_AVAIL_KB / 1024 / 1024 ))
if (( STAGE_AVAIL_GB < 14 )); then
  echo "[ERROR] Not enough free disk space at $STAGE_ROOT (${STAGE_AVAIL_GB} GB free, need >= 14 GB for RootFS build)." >&2
  telemetry_mark_reported
  telemetry_send "install_fail" "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_7_ROOTFS}" "06_build_rootfs.sh" "$LINENO" 28 "insufficient_disk_space" "RootFS staging $STAGE_ROOT has ${STAGE_AVAIL_GB} GB free (need >= 14 GB)"
  exit 1
fi
TMP_WORK="$(mktemp -d "$STAGE_ROOT/yigitzra1n_rootfs.XXXXXX")"
TMP_MNT="$(mktemp -d /private/tmp/yigitzra1n_mnt.XXXXXX)"
MNT_R16="$TMP_MNT/mnt_r16"
MNT_SYS="$TMP_MNT/mnt_sys"
MNT_APP="$TMP_MNT/mnt_app"
MNT_R15="$TMP_MNT/mnt_r15"
MNT_IP8="$TMP_MNT/mnt_ip8"
MNT_IP8_SYS="$TMP_MNT/mnt_ip8_sys"
MNT_IP8_APP="$TMP_MNT/mnt_ip8_app"

detach_if_mounted() {
  local m="$1"
  if [[ -d "$m" ]] && mount | grep -Fq " on $m "; then
    hdiutil detach "$m" >/dev/null 2>&1 || hdiutil detach -force "$m" >/dev/null 2>&1 || true
  fi
}

cleanup() {
  for m in "$MNT_R16" "$MNT_SYS" "$MNT_APP" "$MNT_R15" "$MNT_IP8" "$MNT_IP8_SYS" "$MNT_IP8_APP"; do
    detach_if_mounted "$m"
  done
  rm -rf "$TMP_MNT" "$TMP_WORK"
}
trap cleanup EXIT

extract_zip_entry() {
  local ipsw="$1"
  local member="$2"
  local out_path="$3"
  local label="${4:-$member}"
  mkdir -p "$(dirname "$out_path")"
  local err_file=""
  err_file="$(mktemp "${STAGE_ROOT:-/tmp}/.zip_err.XXXXXX")"
  local rc=0
  set +e
  "$VENV_PY" - "$ipsw" "$member" "$out_path" "$label" "$err_file" <<'PY'
import errno, os, shutil, sys, time, zipfile, zlib

ipsw, member, out_path, label, err_file = sys.argv[1:6]
try:
    with zipfile.ZipFile(ipsw, "r") as zf:
        info = zf.getinfo(member)
        total = info.file_size
        done = 0
        t0 = time.monotonic()
        last_print = 0.0
        chunk_sz = 8 * 1024 * 1024
        with zf.open(info, "r") as src, open(out_path, "wb") as dst:
            while True:
                buf = src.read(chunk_sz)
                if not buf:
                    break
                dst.write(buf)
                done += len(buf)
                now = time.monotonic()
                if now - last_print >= 0.25 or done == total:
                    elapsed = max(now - t0, 1e-3)
                    pct = (done * 100.0 / total) if total else 100.0
                    mb_done = done / (1024 * 1024)
                    mb_tot = total / (1024 * 1024)
                    speed = mb_done / elapsed
                    sys.stdout.write(
                        f"\r  [*] Extracting {label}: {pct:5.1f}% ({mb_done:6.1f} / {mb_tot:6.1f} MB) @ {speed:5.1f} MB/s"
                    )
                    sys.stdout.flush()
                    last_print = now
    sys.stdout.write("\n")
    sys.stdout.flush()
except Exception as e:
    sys.stdout.write("\n")
    sys.stdout.flush()
    try:
        free_gb = shutil.disk_usage(os.path.dirname(os.path.abspath(out_path))).free / (1024 ** 3)
    except Exception:
        free_gb = -1.0
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        reason = "disk_full_extract"
    elif isinstance(e, (zipfile.BadZipFile, zlib.error, EOFError, KeyError)):
        reason = "corrupted_ipsw_extract"
    else:
        reason = "extract_zip_failed"
    detail = f"extract {label} ({os.path.basename(ipsw)}): {type(e).__name__}: {e} [free={free_gb:.1f}GB]"
    try:
        with open(err_file, "w", encoding="utf-8") as ef:
            ef.write(f"{reason}\n{detail}")
    except Exception:
        pass
    print(f"[ERROR] Failed extracting {label} from {os.path.basename(ipsw)}: {type(e).__name__}: {e} (free disk: {free_gb:.1f} GB)", file=sys.stderr)
    if reason == "corrupted_ipsw_extract":
        print(f"        The IPSW file looks corrupted/incomplete. Delete it and re-run ./yigitzra1n.sh prepare:\n        {ipsw}", file=sys.stderr)
    elif reason == "disk_full_extract":
        print("        Disk is full. Free at least 15-20 GB and re-run ./yigitzra1n.sh prepare.", file=sys.stderr)
    sys.exit(1)
PY
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    local reason="extract_zip_failed"
    local detail="extract $label failed (exit $rc)"
    if [[ -f "$err_file" ]]; then
      reason="$(sed -n '1p' "$err_file" 2>/dev/null || echo "extract_zip_failed")"
      detail="$(sed -n '2p' "$err_file" 2>/dev/null || echo "$detail")"
    fi
    rm -f "$err_file" 2>/dev/null || true
    telemetry_mark_reported
    telemetry_send "install_fail" "${TELEMETRY_CMD:-prepare}" "${TELEMETRY_STEP:-PREPARE_7_ROOTFS}" "06_build_rootfs.sh" "${BASH_LINENO[0]:-$LINENO}" "$rc" "$reason" "$detail"
    return "$rc"
  fi
  rm -f "$err_file" 2>/dev/null || true
}

mkdir -p "$HYBRID_DIR" "$MNT_R16" "$MNT_SYS" "$MNT_APP" "$MNT_R15" "$MNT_IP8" "$MNT_IP8_SYS" "$MNT_IP8_APP"

echo "=== [1/7] Staging 15.8.8 Firmware/, Restore.plist & Patched BuildManifest.plist ==="
rm -rf "$HYBRID_DIR/Firmware" "$HYBRID_DIR/BuildManifest.plist" "$HYBRID_DIR/Restore.plist"
unzip -q -o "$IPSW_1588" "BuildManifest.plist" "Restore.plist" "Firmware/*" -d "$HYBRID_DIR"
chmod -R u+w "$HYBRID_DIR"

"$VENV_PY" - "$HYBRID_DIR/BuildManifest.plist" <<'PY'
import plistlib, sys
p = sys.argv[1]
with open(p, "rb") as f:
    bm = plistlib.load(f)
for ident in bm.get("BuildIdentities", []):
    info = ident.get("Info", {})
    if "MinimumSystemPartition" in info:
        info["MinimumSystemPartition"] = 9000
    man = ident.get("Manifest", {})
    man.pop("Ap,SystemVolumeCanonicalMetadata", None)
    man.pop("SystemVolume", None)
with open(p, "wb") as f:
    plistlib.dump(bm, f, fmt=plistlib.FMT_XML)
print("[+] Patched BuildManifest.plist (MinimumSystemPartition=9000, SSV keys stripped)")
PY

cp -f "$BUILD_DIR/kernelcache.release.n71_v6.im4p" "$HYBRID_DIR/kernelcache.release.n71"
cp -f "$BUILD_DIR/kernelcache.release.n71_v6.im4p" "$HYBRID_DIR/kernelcache.release.ipad6f"
cp -f "$BUILD_DIR/DeviceTree.n71map_v5.im4p" "$HYBRID_DIR/Firmware/all_flash/DeviceTree.n71map.im4p"
if [[ -f "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" ]]; then
  cp -f "$BUILD_DIR/DeviceTree.n71ap_v5.im4p" "$HYBRID_DIR/Firmware/all_flash/DeviceTree.n71ap.im4p"
fi
cp -f "$BUILD_DIR/098-68700-067.dmg" "$HYBRID_DIR/098-68700-067.dmg"
ln -sf "098-68700-067.dmg" "$HYBRID_DIR/098-68739-067.dmg"
cp -f "$BUILD_DIR/098-68700-067.dmg.trustcache" "$HYBRID_DIR/Firmware/098-68700-067.dmg.trustcache"
cp -f "$BUILD_DIR/098-68700-067.dmg.trustcache" "$HYBRID_DIR/Firmware/098-68739-067.dmg.trustcache"

if [[ -f "$BUILD_DIR/ssh_ramdisk.im4p" && -f "$BUILD_DIR/ssh_ramdisk.trustcache" ]]; then
  rm -rf "$SSH_RD_IPSW_DIR"
  mkdir -p "$SSH_RD_IPSW_DIR"
  cp -a "$HYBRID_DIR/BuildManifest.plist" "$HYBRID_DIR/Restore.plist" "$HYBRID_DIR/Firmware" "$SSH_RD_IPSW_DIR/"
  cp -f "$HYBRID_DIR/kernelcache.release.n71" "$SSH_RD_IPSW_DIR/kernelcache.release.n71"
  cp -f "$HYBRID_DIR/kernelcache.release.ipad6f" "$SSH_RD_IPSW_DIR/kernelcache.release.ipad6f"
  cp -f "$BUILD_DIR/ssh_ramdisk.im4p" "$SSH_RD_IPSW_DIR/098-68700-067.dmg"
  ln -sf "098-68700-067.dmg" "$SSH_RD_IPSW_DIR/098-68739-067.dmg"
  cp -f "$BUILD_DIR/ssh_ramdisk.trustcache" "$SSH_RD_IPSW_DIR/Firmware/098-68700-067.dmg.trustcache"
  cp -f "$BUILD_DIR/ssh_ramdisk.trustcache" "$SSH_RD_IPSW_DIR/Firmware/098-68739-067.dmg.trustcache"
  touch "$SSH_RD_IPSW_DIR/098-68805-067.dmg"
  echo "[+] Staged SSH RamDisk boot directory: $SSH_RD_IPSW_DIR"
fi

echo "=== [2/7] Resolving RootFS & Cryptex DMG paths from the 3 Stock IPSWs ==="
resolve_ipsw_dmgs() {
  "$VENV_PY" - "$1" <<'PY'
import plistlib, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as zf:
    bm = plistlib.loads(zf.read("BuildManifest.plist"))
    ident = bm["BuildIdentities"][0]
    man = ident["Manifest"]
    os_dmg = man["OS"]["Info"]["Path"]
    sys_cx = man.get("Cryptex1,SystemOS", {}).get("Info", {}).get("Path", "")
    app_cx = man.get("Cryptex1,AppOS", {}).get("Info", {}).get("Path", "")
    print(f"{os_dmg}|{sys_cx}|{app_cx}")
PY
}

IFS='|' read -r IPAD_OS IPAD_SYS IPAD_APP <<< "$(resolve_ipsw_dmgs "$IPSW_IPAD5")"
IFS='|' read -r I15_OS _ _ <<< "$(resolve_ipsw_dmgs "$IPSW_1588")"
IFS='|' read -r IP8_OS IP8_SYS IP8_APP <<< "$(resolve_ipsw_dmgs "$IPSW_IP8")"

mkdir -p "$TMP_WORK/dmgs"

echo "=== [3/7] Creating Writable Hybrid RootFS Sparseimage (8.5 GB) & Merging iPad 5 Cryptexes ==="
extract_zip_entry "$IPSW_IPAD5" "$IPAD_OS" "$TMP_WORK/dmgs/ipad5_os.dmg" "iPad 5 RootFS ($IPAD_OS)"
SPARSE="$TMP_WORK/hybrid_rootfs.sparseimage"
echo "  [*] Converting iPad 5 RootFS to writable sparseimage..."
hdiutil convert "$TMP_WORK/dmgs/ipad5_os.dmg" -format UDSP -o "$SPARSE"
rm -f "$TMP_WORK/dmgs/ipad5_os.dmg"
hdiutil resize -size 8500m "$SPARSE" >/dev/null

hdiutil attach -owners on -readwrite -noverify -noautoopen -mountpoint "$MNT_R16" "$SPARSE" >/dev/null

extract_zip_entry "$IPSW_IPAD5" "$IPAD_SYS" "$TMP_WORK/dmgs/ipad5_sys.dmg" "iPad 5 Cryptex SystemOS ($IPAD_SYS)"
extract_zip_entry "$IPSW_IPAD5" "$IPAD_APP" "$TMP_WORK/dmgs/ipad5_app.dmg" "iPad 5 Cryptex AppOS ($IPAD_APP)"
hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_SYS" "$TMP_WORK/dmgs/ipad5_sys.dmg" >/dev/null
hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_APP" "$TMP_WORK/dmgs/ipad5_app.dmg" >/dev/null

echo "  [*] Merging SystemOS + AppOS Cryptexes into Hybrid RootFS..."
rm -rf "$MNT_R16/System/Cryptexes/OS" "$MNT_R16/System/Cryptexes/App"
mkdir -p "$MNT_R16/System/Cryptexes/OS" "$MNT_R16/System/Cryptexes/App"
chown root:wheel "$MNT_R16/System/Cryptexes/OS" "$MNT_R16/System/Cryptexes/App"
chmod 755 "$MNT_R16/System/Cryptexes/OS" "$MNT_R16/System/Cryptexes/App"
ditto "$MNT_SYS/" "$MNT_R16/System/Cryptexes/OS/"
ditto "$MNT_APP/" "$MNT_R16/System/Cryptexes/App/"

detach_if_mounted "$MNT_SYS"
detach_if_mounted "$MNT_APP"
rm -f "$TMP_WORK/dmgs/ipad5_sys.dmg" "$TMP_WORK/dmgs/ipad5_app.dmg"

if [[ ! -e "$MNT_R16/System/Library/Caches/com.apple.dyld" ]]; then
  ln -s ../../Cryptexes/OS/System/Library/Caches/com.apple.dyld "$MNT_R16/System/Library/Caches/com.apple.dyld"
fi

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -e "$src" ]]; then
    mkdir -p "$(dirname "$dst")"
    ditto "$src" "$dst"
  fi
}

echo "=== [4/7] Copying iPhone 6s 15.8.8 Hardware Firmware, Thermal Profile, Camera ISP Profiles, Audio Tunings & AOP ==="
extract_zip_entry "$IPSW_1588" "$I15_OS" "$TMP_WORK/dmgs/i15_os.dmg" "iPhone 6s 15.8.8 RootFS ($I15_OS)"
hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_R15" "$TMP_WORK/dmgs/i15_os.dmg" >/dev/null
rsync -a --ignore-existing "$MNT_R15/usr/share/firmware/" "$MNT_R16/usr/share/firmware/"
copy_if_exists "$MNT_R15/System/Library/ThermalMonitor/N71mAP-Info.plist" "$MNT_R16/System/Library/ThermalMonitor/N71mAP-Info.plist"
copy_if_exists "$MNT_R15/System/Library/PrivateFrameworks/CMCapture.framework/N71" "$MNT_R16/System/Library/PrivateFrameworks/CMCapture.framework/N71"
copy_if_exists "$MNT_R15/System/Library/PrivateFrameworks/CMCapture.framework/N71m" "$MNT_R16/System/Library/PrivateFrameworks/CMCapture.framework/N71m"
copy_if_exists "$MNT_R15/Library/Audio/Tunings/N71" "$MNT_R16/Library/Audio/Tunings/N71"
if [[ -d "$MNT_R16/Library/Audio/Tunings/N71" && ! -e "$MNT_R16/Library/Audio/Tunings/N71m" ]]; then
  ln -sfn "N71" "$MNT_R16/Library/Audio/Tunings/N71m"
fi

mkdir -p "$MNT_R16/usr/local/share/firmware" "$MNT_R16/usr/local/share/backups" "$MNT_R16/usr/local/libexec" "$MNT_R16/usr/local/bin"

"$VENV_PY" - "$HYBRID_DIR/Firmware/AOP/aopfw-s8000aop.im4p" "$MNT_R16/usr/local/share/firmware/s8000aop.bin" <<'PY'
import pyimg4, sys
im = pyimg4.IM4P(open(sys.argv[1], "rb").read())
if im.payload.compression != pyimg4.Compression.NONE:
    im.payload.decompress()
open(sys.argv[2], "wb").write(im.payload.data)
print(f"[+] Extracted s8000aop.bin ({len(im.payload.data)} B)")
PY
chown root:wheel "$MNT_R16/usr/local/share/firmware/s8000aop.bin"
chmod 644 "$MNT_R16/usr/local/share/firmware/s8000aop.bin"

detach_if_mounted "$MNT_R15"
rm -f "$TMP_WORK/dmgs/i15_os.dmg"

echo "=== [5/7] Integrating iPhone 8 (16.7.16) iPhone UI Layer, Keyboard, Wallpapers & Ringtones ==="
extract_zip_entry "$IPSW_IP8" "$IP8_OS" "$TMP_WORK/dmgs/ip8_os.dmg" "iPhone 8 RootFS ($IP8_OS)"
hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_IP8" "$TMP_WORK/dmgs/ip8_os.dmg" >/dev/null
if [[ -n "$IP8_SYS" ]]; then
  extract_zip_entry "$IPSW_IP8" "$IP8_SYS" "$TMP_WORK/dmgs/ip8_sys.dmg" "iPhone 8 Cryptex SystemOS ($IP8_SYS)"
  hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_IP8_SYS" "$TMP_WORK/dmgs/ip8_sys.dmg" >/dev/null
fi
if [[ -n "$IP8_APP" ]]; then
  extract_zip_entry "$IPSW_IP8" "$IP8_APP" "$TMP_WORK/dmgs/ip8_app.dmg" "iPhone 8 Cryptex AppOS ($IP8_APP)"
  hdiutil attach -readonly -noverify -noautoopen -mountpoint "$MNT_IP8_APP" "$TMP_WORK/dmgs/ip8_app.dmg" >/dev/null
fi
for app in MobilePhone.app FTMInternal-4.app Passbook.app Calculator.app Compass.app; do
  if [[ -d "$MNT_IP8/Applications/$app" ]]; then
    copy_if_exists "$MNT_IP8/Applications/$app" "$MNT_R16/Applications/$app"
  elif [[ -d "$MNT_IP8_APP/Applications/$app" ]]; then
    copy_if_exists "$MNT_IP8_APP/Applications/$app" "$MNT_R16/Applications/$app"
  fi
done

copy_if_exists "$MNT_IP8/System/Library/CoreServices/AegirProxyApp.app" "$MNT_R16/System/Library/CoreServices/AegirProxyApp.app"
copy_if_exists "$MNT_IP8/System/Library/CoreServices/SpringBoard.app/Assets.car" "$MNT_R16/System/Library/CoreServices/SpringBoard.app/Assets.car"
for f in "$MNT_IP8"/System/Library/CoreServices/SpringBoard.app/DefaultIconState*iphone*.plist; do
  [[ -f "$f" ]] && cp -f "$f" "$MNT_R16/System/Library/CoreServices/SpringBoard.app/"
done

copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/SpringBoardHome.framework/Assets.car" "$MNT_R16/System/Library/PrivateFrameworks/SpringBoardHome.framework/Assets.car"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/ControlCenterServices.framework/ModuleAllowedList.plist" "$MNT_R16/System/Library/PrivateFrameworks/ControlCenterServices.framework/ModuleAllowedList.plist"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/ControlCenterUI.framework/DefaultModuleSettings~iphone.plist" "$MNT_R16/System/Library/PrivateFrameworks/ControlCenterUI.framework/DefaultModuleSettings~iphone.plist"
copy_if_exists "$MNT_IP8/System/Library/PreferenceBundles/CompassSettings.bundle" "$MNT_R16/System/Library/PreferenceBundles/CompassSettings.bundle"
copy_if_exists "$MNT_IP8/System/Library/PreferenceBundles/MobilePhoneSettings.bundle" "$MNT_R16/System/Library/PreferenceBundles/MobilePhoneSettings.bundle"

copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/TextInputUI.framework/Assets.car" "$MNT_R16/System/Library/PrivateFrameworks/TextInputUI.framework/Assets.car"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/TextInputUI.framework/KBLayouts_iPhone.dat" "$MNT_R16/System/Library/PrivateFrameworks/TextInputUI.framework/KBLayouts_iPhone.dat"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/UIKitCore.framework/Artwork.bundle/Assets.car" "$MNT_R16/System/Library/PrivateFrameworks/UIKitCore.framework/Artwork.bundle/Assets.car"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/Assets.car" "$MNT_R16/System/Library/PrivateFrameworks/UIKitCore.framework/BoundingPathData.bundle/Assets.car"

for cc_bundle in ContinuousExposeModule.bundle LowPowerModule.bundle SilenceCallsCCWidget.bundle; do
  copy_if_exists "$MNT_IP8/System/Library/ControlCenter/Bundles/$cc_bundle/Assets.car" \
                 "$MNT_R16/System/Library/ControlCenter/Bundles/$cc_bundle/Assets.car"
done

copy_if_exists "$MNT_IP8/Library/Wallpaper/DefaultWallpapers~iphone.plist" "$MNT_R16/Library/Wallpaper/DefaultWallpapers~iphone.plist"
copy_if_exists "$MNT_IP8/Library/Wallpaper/Collections/Collections~iphone.plist" "$MNT_R16/Library/Wallpaper/Collections/Collections~iphone.plist"
copy_if_exists "$MNT_IP8/Library/Wallpaper/Collections/iOS_16~iphone.wallpaperCollection" "$MNT_R16/Library/Wallpaper/Collections/iOS_16~iphone.wallpaperCollection"
copy_if_exists "$MNT_IP8/Library/Wallpaper/Collections/Clownfish~iphone.wallpaperCollection" "$MNT_R16/Library/Wallpaper/Collections/Clownfish~iphone.wallpaperCollection"
copy_if_exists "$MNT_IP8/Library/Wallpaper/Collections/Stripe.wallpaperCollection/Wallpapers" "$MNT_R16/Library/Wallpaper/Collections/Stripe.wallpaperCollection/Wallpapers"
rsync -a --ignore-existing --include='*/' --include='*667h*' --include='*568h*' --include='*~iphone*' --exclude='*' \
  "$MNT_IP8/System/Library/ProceduralWallpaper/" "$MNT_R16/System/Library/ProceduralWallpaper/" || true

copy_if_exists "$MNT_IP8/Library/Ringtones/Reflection.m4r" "$MNT_R16/Library/Ringtones/Reflection.m4r"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/ToneLibrary.framework/Vibrations/Synchronized/Reflection.plist" "$MNT_R16/System/Library/PrivateFrameworks/ToneLibrary.framework/Vibrations/Synchronized/Reflection.plist"
copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/ToneKit.framework/TKRingtones.plist" "$MNT_R16/System/Library/PrivateFrameworks/ToneKit.framework/TKRingtones.plist"

if [[ -d "$MNT_IP8/System/Library/PrivateFrameworks/WatchFacesWallpaperSupport.framework" && -d "$MNT_IP8_SYS/System/Library/Caches/com.apple.dyld" ]]; then
  WF_DST="$MNT_R16/System/Library/PrivateFrameworks/WatchFacesWallpaperSupport.framework"
  copy_if_exists "$MNT_IP8/System/Library/PrivateFrameworks/WatchFacesWallpaperSupport.framework" "$WF_DST"
  rm -rf "$WF_DST/_CodeSignature"
  "$VENV_PY" "$ROOT_DIR/scripts/build_watchfaces_support.py" \
    --ip8-cache-dir "$MNT_IP8_SYS/System/Library/Caches/com.apple.dyld" \
    --r16-cache-dir "$MNT_R16/System/Cryptexes/OS/System/Library/Caches/com.apple.dyld" \
    --out "$WF_DST/WatchFacesWallpaperSupport"
  chown -R root:wheel "$WF_DST"
  chmod 755 "$WF_DST/WatchFacesWallpaperSupport"
fi

rsync -a --ignore-existing --include='*/' --include='*~iphone*' --include='*667h*' --include='*568h*' --exclude='*' \
  "$MNT_IP8/System/Library/PrivateFrameworks/" "$MNT_R16/System/Library/PrivateFrameworks/" || true
rsync -a --ignore-existing --include='*/' --include='*~iphone*' --include='*667h*' --include='*568h*' --exclude='*' \
  "$MNT_IP8/System/Library/Frameworks/" "$MNT_R16/System/Library/Frameworks/" || true

detach_if_mounted "$MNT_IP8"
detach_if_mounted "$MNT_IP8_SYS"
detach_if_mounted "$MNT_IP8_APP"
rm -f "$TMP_WORK/dmgs/ip8_os.dmg" "$TMP_WORK/dmgs/ip8_sys.dmg" "$TMP_WORK/dmgs/ip8_app.dmg"

echo "=== [6/7] Installing Daemons, Dylibs, Signed Procursus OpenSSH & Patching launchd.plist ==="
cp -f "$BIN_DIR/sn2400_charger_daemon" "$MNT_R16/usr/local/libexec/sn2400_charger_daemon"
cp -f "$BIN_DIR/ios16_sshd_launcher" "$MNT_R16/usr/local/libexec/ios16_sshd_launcher"
cp -f "$BIN_DIR/sleepd_stub" "$MNT_R16/usr/libexec/sleepd_stub"
cp -f "$BIN_DIR/libn71m_avgestalt.dylib" "$MNT_R16/usr/lib/libn71m_avgestalt.dylib"
cp -f "$BIN_DIR/libn71m_tipsmodel.dylib" "$MNT_R16/usr/lib/libn71m_tipsmodel.dylib"
chown root:wheel \
  "$MNT_R16/usr/local/libexec/sn2400_charger_daemon" \
  "$MNT_R16/usr/local/libexec/ios16_sshd_launcher" \
  "$MNT_R16/usr/libexec/sleepd_stub" \
  "$MNT_R16/usr/lib/libn71m_avgestalt.dylib" \
  "$MNT_R16/usr/lib/libn71m_tipsmodel.dylib"
chmod 755 \
  "$MNT_R16/usr/local/libexec/sn2400_charger_daemon" \
  "$MNT_R16/usr/local/libexec/ios16_sshd_launcher" \
  "$MNT_R16/usr/libexec/sleepd_stub" \
  "$MNT_R16/usr/lib/libn71m_avgestalt.dylib" \
  "$MNT_R16/usr/lib/libn71m_tipsmodel.dylib"

(
  set +e
  mkdir -p "$MNT_R16/usr/local/share/yigitzra1n"
  "$VENV_PY" "$ROOT_DIR/scripts/build_statusbar_overrides.py" \
    --out "$MNT_R16/usr/local/share/yigitzra1n/statusBarOverrides" 2>/dev/null || true
  if [[ -f "$MNT_R16/usr/local/share/yigitzra1n/statusBarOverrides" ]]; then
    chown root:wheel "$MNT_R16/usr/local/share/yigitzra1n/statusBarOverrides" 2>/dev/null || true
    chmod 644 "$MNT_R16/usr/local/share/yigitzra1n/statusBarOverrides" 2>/dev/null || true
  fi
) || true

patch_offline_macho() {
  local target="$1"
  local bkp="$2"
  local dylib="$3"
  local bid="$4"
  [[ -f "$target" ]] || return 0
  cp -p "$target" "$MNT_R16/usr/local/share/backups/$bkp"
  codesign -d --entitlements - --xml "$target" 2>/dev/null > "$TMP_WORK/$bkp.ent.plist" || true
  "$VENV_PY" "$ROOT_DIR/scripts/inject_dylib.py" "$target" "$dylib" "$TMP_WORK/$bkp.patched"
  if [[ -s "$TMP_WORK/$bkp.ent.plist" ]]; then
    codesign -f -s - -i "$bid" --entitlements "$TMP_WORK/$bkp.ent.plist" "$TMP_WORK/$bkp.patched"
  else
    codesign -f -s - -i "$bid" "$TMP_WORK/$bkp.patched"
  fi
  rm -f "$target"
  mv "$TMP_WORK/$bkp.patched" "$target"
  chown root:wheel "$target"
  chmod 755 "$target"
}

patch_offline_macho "$MNT_R16/Applications/Camera.app/Camera" "Camera.orig" "/usr/lib/libn71m_avgestalt.dylib" "com.apple.camera"
patch_offline_macho "$MNT_R16/Applications/Preferences.app/Preferences" "Preferences.orig" "/usr/lib/libn71m_avgestalt.dylib" "com.apple.Preferences"
patch_offline_macho "$MNT_R16/usr/libexec/tipsd" "tipsd.orig" "/usr/lib/libn71m_tipsmodel.dylib" "com.apple.tipsd"

if [[ -f "$MNT_R16/usr/libexec/securityd" ]]; then
  cp -p "$MNT_R16/usr/libexec/securityd" "$MNT_R16/usr/local/share/backups/securityd.orig"
  "$VENV_PY" "$ROOT_DIR/scripts/patch_securityd.py" \
    --in "$MNT_R16/usr/libexec/securityd" \
    --soft-aks-obj "$BIN_DIR/soft_aks.o" \
    --entitlements "$ROOT_DIR/entitlements/securityd.entitlements.plist" \
    --out "$TMP_WORK/securityd.patched"
  rm -f "$MNT_R16/usr/libexec/securityd"
  mv "$TMP_WORK/securityd.patched" "$MNT_R16/usr/libexec/securityd"
  chown root:wheel "$MNT_R16/usr/libexec/securityd"
  chmod 755 "$MNT_R16/usr/libexec/securityd"
fi

if [[ -f "$MNT_R16/usr/libexec/locationd" ]]; then
  cp -p "$MNT_R16/usr/libexec/locationd" "$MNT_R16/usr/local/share/backups/locationd.orig"
  "$VENV_PY" "$ROOT_DIR/scripts/patch_locationd.py" \
    --in "$MNT_R16/usr/libexec/locationd" \
    --out "$TMP_WORK/locationd.patched"
  rm -f "$MNT_R16/usr/libexec/locationd"
  mv "$TMP_WORK/locationd.patched" "$MNT_R16/usr/libexec/locationd"
  chown root:wheel "$MNT_R16/usr/libexec/locationd"
  chmod 755 "$MNT_R16/usr/libexec/locationd"
fi

ZSTD_BIN="$(command -v zstd || echo /opt/homebrew/bin/zstd)"
BS_ZST="$PROCURSUS_DIR/bootstrap-iphoneos-arm64.tar.zst"
if [[ -f "$BS_ZST" ]]; then
  SSH_STAGE="$TMP_WORK/ssh_stage"
  mkdir -p "$SSH_STAGE"
  "$ZSTD_BIN" -dc "$BS_ZST" | tar -C "$SSH_STAGE" -xf -
  if [[ -d "$SSH_STAGE/var/jb" ]]; then
    "$VENV_PY" "$ROOT_DIR/scripts/stage_procursus_debs.py" \
      --stage-root "$SSH_STAGE/var/jb" \
      --deb-dir "$PROCURSUS_DIR/debs" \
      --fallback-ent "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist"

    if [[ -d "$SSH_STAGE/var/jb/Applications/Sileo.app" ]]; then
      rm -rf "$MNT_R16/Applications/Sileo.app"
      ditto "$SSH_STAGE/var/jb/Applications/Sileo.app" "$MNT_R16/Applications/Sileo.app"
      chown -R root:wheel "$MNT_R16/Applications/Sileo.app"
      chmod 755 "$MNT_R16/Applications/Sileo.app" "$MNT_R16/Applications/Sileo.app/Sileo" 2>/dev/null || true
      if [[ -f "$BIN_DIR/yigitzra1n_givemeroot" ]]; then
        cp -f "$BIN_DIR/yigitzra1n_givemeroot" "$MNT_R16/Applications/Sileo.app/giveMeRoot"
      fi
      if [[ -f "$MNT_R16/Applications/Sileo.app/giveMeRoot" ]]; then
        chown root:wheel "$MNT_R16/Applications/Sileo.app/giveMeRoot"
        chmod 4755 "$MNT_R16/Applications/Sileo.app/giveMeRoot"
      fi
      rm -rf "$SSH_STAGE/var/jb/Applications/Sileo.app"
      ln -sfn "/Applications/Sileo.app" "$SSH_STAGE/var/jb/Applications/Sileo.app"
      echo "[+] Installed Sileo.app into /Applications/Sileo.app (giveMeRoot setuid) + /var/jb/Applications symlink"
    fi

    rm -rf "$MNT_R16/usr/local/procursus"
    mkdir -p "$MNT_R16/usr/local/procursus"
    ditto "$SSH_STAGE/var/jb" "$MNT_R16/usr/local/procursus"
    chown -R root:wheel "$MNT_R16/usr/local/procursus"
    mkdir -p "$MNT_R16/usr/local/procursus/var/mobile/Library/Caches" "$MNT_R16/usr/local/procursus/var/mobile/Library/Preferences" "$MNT_R16/usr/local/procursus/etc/apt/sources.list.d"
    cat > "$MNT_R16/usr/local/procursus/etc/apt/sources.list.d/sileo.sources" <<'SRCS'
Types: deb
URIs: https://havoc.app/
Suites: ./
Components:

Types: deb
URIs: https://repo.chariz.com/
Suites: ./
Components:

Types: deb
URIs: https://ellekit.space/
Suites: ./
Components:
Trusted: yes

Types: deb
URIs: https://opa334.github.io/
Suites: ./
Components:
Trusted: yes

Types: deb
URIs: http://apt.thebigboss.org/repofiles/cydia/
Suites: stable
Components: main
Trusted: yes
SRCS
    chown root:wheel "$MNT_R16/usr/local/procursus/etc/apt/sources.list.d/sileo.sources"
    chmod 644 "$MNT_R16/usr/local/procursus/etc/apt/sources.list.d/sileo.sources"
    : > "$MNT_R16/usr/local/procursus/.yigitzra1n_repos_ready"
    : > "$MNT_R16/usr/local/procursus/.yigitzra1n_repos_v2_ready"
    chown -R 501:501 "$MNT_R16/usr/local/procursus/var/mobile"
    chmod 755 "$MNT_R16/usr/local/procursus/var/mobile" "$MNT_R16/usr/local/procursus/var/mobile/Library" "$MNT_R16/usr/local/procursus/var/mobile/Library/Caches" "$MNT_R16/usr/local/procursus/var/mobile/Library/Preferences"
    chmod 600 "$MNT_R16/usr/local/procursus/etc/master.passwd" 2>/dev/null || true
    chmod 755 "$MNT_R16/usr/local/procursus/var/empty" 2>/dev/null || true

    ln -sf "/var/jb/usr/bin/bash" "$MNT_R16/bin/sh"
    ln -sf "/var/jb/usr/bin/bash" "$MNT_R16/bin/bash"
    ln -sf "/var/jb/usr/libexec/sftp-server" "$MNT_R16/usr/libexec/sftp-server"
    ln -sfn "/var/jb/etc/apt" "$MNT_R16/private/etc/apt"
    for tool in env apt apt-get apt-cache dpkg plutil uicache sbreload; do
      [[ -e "$MNT_R16/usr/bin/$tool" ]] || ln -sf "/var/jb/usr/bin/$tool" "$MNT_R16/usr/bin/$tool"
    done
    cat > "$MNT_R16/private/etc/profile" <<'PROF'
export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PROF
    chown root:wheel "$MNT_R16/private/etc/profile"
    chmod 644 "$MNT_R16/private/etc/profile"
    echo "[+] Embedded signed Procursus OpenSSH + Sileo + uikittools & RootFS symlinks"
  fi
fi

cp -p "$MNT_R16/System/Library/xpc/launchd.plist" "$MNT_R16/usr/local/share/backups/launchd.plist.orig"
"$VENV_PY" - "$MNT_R16/System/Library/xpc/launchd.plist" "$MNT_R16/System/Library/LaunchDaemons/com.openssh.sshd.plist" <<'PY'
import plistlib, sys

ld_path, ssh_path = sys.argv[1], sys.argv[2]
with open(ld_path, "rb") as f:
    d = plistlib.load(f)

daemons = d.setdefault("LaunchDaemons", {})

sshd_dict = {
    "Label": "com.openssh.sshd",
    "ProgramArguments": ["/usr/local/libexec/ios16_sshd_launcher"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "EnablePressuredExit": False,
    "EnableTransactions": False,
    "StandardErrorPath": "/var/log/sshd.err",
    "UserName": "root",
}
daemons["/System/Library/LaunchDaemons/com.openssh.sshd.plist"] = sshd_dict

daemons["/System/Library/LaunchDaemons/com.apple.sleepd.plist"] = {
    "Label": "com.apple.sleepd",
    "ProgramArguments": ["/usr/libexec/sleepd_stub"],
    "MachServices": {
        "com.apple.private.alloy.sleep.classd-idswake": True,
        "com.apple.sleepd.sleepserver": True,
    },
    "POSIXSpawnType": "Interactive",
    "RunAtLoad": True,
    "KeepAlive": {"Crashed": True},
    "EnablePressuredExit": False,
    "EnableTransactions": False,
    "UserName": "mobile",
}

daemons["/System/Library/LaunchDaemons/com.apple.sn2400charger.plist"] = {
    "Label": "com.apple.sn2400charger",
    "ProgramArguments": ["/usr/local/libexec/sn2400_charger_daemon", "1000"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "UserName": "root",
}

wfl = daemons.get("/System/Library/LaunchDaemons/com.apple.wifiFirmwareLoader.plist")
if isinstance(wfl, dict):
    wfl["RunAtLoad"] = True

with open(ld_path, "wb") as f:
    plistlib.dump(d, f, fmt=plistlib.FMT_BINARY)
with open(ssh_path, "wb") as f:
    plistlib.dump(sshd_dict, f, fmt=plistlib.FMT_BINARY)
print("[+] Patched launchd.plist (native ios16_sshd_launcher :22, sleepd_stub, sn2400_charger_daemon, wifiFirmwareLoader)")
PY
chown root:wheel "$MNT_R16/System/Library/xpc/launchd.plist" "$MNT_R16/System/Library/LaunchDaemons/com.openssh.sshd.plist"
chmod 644 "$MNT_R16/System/Library/xpc/launchd.plist" "$MNT_R16/System/Library/LaunchDaemons/com.openssh.sshd.plist"

echo "=== [7/7] Detaching, Converting to UDZO DMG & Running asr imagescan ==="
hdiutil detach "$MNT_R16" >/dev/null
OUT_DMG="$HYBRID_DIR/098-68805-067.dmg"
rm -f "$OUT_DMG"
echo "  [*] Compressing Hybrid RootFS to UDZO DMG (writing to $OUT_DMG)..."
hdiutil convert "$SPARSE" -format UDZO -o "$OUT_DMG"
rm -f "$SPARSE"
echo "  [*] Running asr imagescan..."
asr imagescan --source "$OUT_DMG"

if [[ -n "${SUDO_USER:-}" ]]; then
  chown -R "${SUDO_USER}:staff" "$HYBRID_DIR" "$SSH_RD_IPSW_DIR" "$BIN_DIR" "$BUILD_DIR/.tmp" 2>/dev/null || true
fi

echo "[+] Hybrid RootFS & restore directory ready: $HYBRID_DIR"
ls -lh "$OUT_DMG"
