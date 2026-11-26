#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROCURSUS_DIR="${PROCURSUS_DIR:-$ROOT_DIR/.cache/procursus}"
BUILD_DIR="$ROOT_DIR/build"
SSH_PASS="${SSH_PASS:-alpine}"
DEVICE_HOST="${DEVICE_HOST:-127.0.0.1}"
DEVICE_PORT="${DEVICE_PORT:-}"
SILEO_REQUIRE_SSH="${SILEO_REQUIRE_SSH:-0}"
SILEO_WAIT_TRIES="${SILEO_WAIT_TRIES:-75}"

PY_BIN="$ROOT_DIR/.venv/bin/python3"
[[ -x "$PY_BIN" ]] || PY_BIN="$(command -v python3)"

if [[ -f "$ROOT_DIR/scripts/telemetry.sh" ]]; then
  # shellcheck source=scripts/telemetry.sh
  source "$ROOT_DIR/scripts/telemetry.sh"
fi

report_sileo_fail() {
  local line="${1:-0}"
  local rc="${2:-1}"
  local reason="${3:-sileo_failed}"
  local msg="${4:-}"
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]] && command -v telemetry_send >/dev/null 2>&1; then
    telemetry_was_reported && return 0
    telemetry_mark_reported
    telemetry_send "install_fail" "${TELEMETRY_CMD:-sileo}" "SILEO" "provision_sileo.sh" "$line" "$rc" "$reason" "$msg"
  fi
}

for req in sshpass ssh lsof tar codesign; do
  if ! command -v "$req" >/dev/null 2>&1; then
    if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
      echo "[ERROR] Missing required host tool: '$req'." >&2
      report_sileo_fail "$LINENO" 1 "missing_host_tool" "Missing host tool: $req"
      exit 1
    fi
    echo "[!] Sileo auto-install skipped: missing '$req'." >&2
    exit 0
  fi
done

mkdir -p "$BUILD_DIR/.tmp" "$PROCURSUS_DIR/debs"
WORK_TMP="$(mktemp -d /tmp/yigitz_sileo.XXXXXX)"
IPROXY_PID=""

cleanup() {
  if [[ -n "$IPROXY_PID" ]]; then
    kill "$IPROXY_PID" 2>/dev/null || true
    wait "$IPROXY_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK_TMP"
}
trap cleanup EXIT

detect_target_udid() {
  local uid ptype
  if command -v idevice_id >/dev/null 2>&1 && command -v ideviceinfo >/dev/null 2>&1; then
    while IFS= read -r uid; do
      [[ -n "$uid" ]] || continue
      ptype="$(ideviceinfo -u "$uid" -k ProductType 2>/dev/null || true)"
      if [[ "$ptype" == "iPhone8,1" ]]; then
        printf '%s' "$uid"
        return 0
      fi
    done < <(idevice_id -l 2>/dev/null || true)
  fi
  return 1
}

if [[ -z "$DEVICE_PORT" ]]; then
  DEVICE_PORT=2228
  while lsof -iTCP:"$DEVICE_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
    DEVICE_PORT=$((DEVICE_PORT + 1))
  done
fi

start_iproxy_for_udid() {
  local uid="${1:-}"
  [[ -n "$IPROXY_PID" ]] && return 0
  if [[ "$DEVICE_HOST" != "127.0.0.1" && "$DEVICE_HOST" != "localhost" ]]; then
    return 0
  fi
  command -v iproxy >/dev/null 2>&1 || return 0
  local iproxy_args=()
  [[ -n "$uid" ]] && iproxy_args+=(-u "$uid")
  if iproxy --help 2>&1 | grep -q "LOCAL_PORT:DEVICE_PORT"; then
    iproxy ${iproxy_args[@]+"${iproxy_args[@]}"} "${DEVICE_PORT}:22" >/dev/null 2>&1 &
  else
    iproxy ${iproxy_args[@]+"${iproxy_args[@]}"} "$DEVICE_PORT" 22 >/dev/null 2>&1 &
  fi
  IPROXY_PID=$!
  sleep 0.5
}

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o PreferredAuthentications=password -o ConnectTimeout=3 -p "$DEVICE_PORT")
run_ssh() { sshpass -p "$SSH_PASS" ssh "${SSH_OPTS[@]}" "root@${DEVICE_HOST}" "$@"; }

echo "[*] Waiting for iOS SSH to check/install Sileo & default repos (Ctrl+C to skip)..."
ssh_ready=0
TARGET_UDID=""
for (( _try=1; _try<=SILEO_WAIT_TRIES; _try++ )); do
  if [[ -z "$TARGET_UDID" ]]; then
    TARGET_UDID="$(detect_target_udid || true)"
    if [[ -n "$TARGET_UDID" ]]; then
      start_iproxy_for_udid "$TARGET_UDID"
    elif [[ "$_try" -ge 15 && -z "$IPROXY_PID" ]]; then
      start_iproxy_for_udid ""
    fi
  fi
  if [[ -n "$IPROXY_PID" || ( "$DEVICE_HOST" != "127.0.0.1" && "$DEVICE_HOST" != "localhost" ) ]]; then
    if run_ssh "true" >/dev/null 2>&1; then
      ssh_ready=1
      break
    fi
  fi
  sleep 2
done

if [[ "$ssh_ready" -ne 1 ]]; then
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    echo "[ERROR] Device SSH did not answer within $((SILEO_WAIT_TRIES * 2))s. Make sure your iPhone 6s is booted into iOS 16 and connected via USB." >&2
    report_sileo_fail "$LINENO" 1 "sileo_ssh_timeout" "Device SSH did not answer within $((SILEO_WAIT_TRIES * 2))s"
    exit 1
  fi
  echo "[!] Device SSH did not answer within $((SILEO_WAIT_TRIES * 2))s — Sileo check skipped (non-fatal)." >&2
  exit 0
fi

already_ok=0
if run_ssh '
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  test -x /Applications/Sileo.app/Sileo &&
  test -u /Applications/Sileo.app/giveMeRoot &&
  test -L /var/jb/Applications/Sileo.app &&
  test -x /var/jb/usr/bin/uicache &&
  test -f /private/var/jb/.yigitzra1n_sileo_ready &&
  test -f /private/var/jb/.yigitzra1n_fw_ready &&
  test -f /private/var/jb/.yigitzra1n_repos_v2_ready &&
  test -S /var/run/yigitzra1n_gmr.sock &&
  grep -qa "yigitzra1n_gmr" /Applications/Sileo.app/giveMeRoot 2>/dev/null &&
  grep -qa "yigitzra1n_gmr_daemon_v2" /usr/local/libexec/ios16_sshd_launcher 2>/dev/null &&
  test -f /private/var/mobile/Library/SpringBoard/statusBarOverrides &&
  [ "$(wc -c < /private/var/mobile/Library/SpringBoard/statusBarOverrides 2>/dev/null | tr -d " ")" = "3944" ] &&
  [ "$(stat -c "%u" /var/jb/var/mobile 2>/dev/null || stat -f "%u" /var/jb/var/mobile 2>/dev/null)" = "501" ] &&
  grep -qa "com.apple.private.security.no-sandbox" /Applications/Sileo.app/Sileo 2>/dev/null &&
  grep -q "^Package: org.coolstar.sileo$" /var/jb/Library/dpkg/status 2>/dev/null &&
  test -d /private/var/jb/var/lib/apt/sileolists/partial &&
  test -s /private/var/jb/var/lib/apt/lists/apt.thebigboss.org_repofiles_cydia_dists_stable_main_binary-iphoneos-arm64_Packages &&
  test -s /private/var/jb/var/lib/apt/sileolists/apt.thebigboss.org_repofiles_cydia_dists_stable_main_binary-iphoneos-arm64_Packages &&
  test -s /private/var/jb/var/lib/apt/sileolists/repo.chariz.com_._Packages
' >/dev/null 2>&1; then
  already_ok=1
fi

if [[ "$already_ok" -eq 1 ]]; then
  echo "[+] Sileo & default repos already installed."
  exit 0
fi

if ! run_ssh '[ -L /var ] || [ ! -e /var ]'; then
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    echo "[!] Device /var is not a symlink (expected /var -> private/var). Repair /var via ssh-ramdisk before provisioning Sileo." >&2
    report_sileo_fail "$LINENO" 1 "sileo_var_not_symlink" "Device /var is not a symlink"
    exit 1
  fi
  echo "[!] Device /var check failed — Sileo setup skipped (non-fatal)." >&2
  exit 0
fi

find_latest_deb() {
  local pat="$1"
  "$PY_BIN" - "$PROCURSUS_DIR/debs" "$pat" <<'PY' 2>/dev/null || true
import fnmatch, os, re, sys
debs_dir, pat = sys.argv[1], sys.argv[2]
if not os.path.isdir(debs_dir):
    sys.exit(0)
matches = [f for f in os.listdir(debs_dir) if fnmatch.fnmatch(f, pat) and os.path.isfile(os.path.join(debs_dir, f))]
if not matches:
    sys.exit(0)
def key_fn(name):
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", name)]
print(os.path.join(debs_dir, sorted(matches, key=key_fn)[-1]))
PY
}

UK_DEB="$(find_latest_deb 'uikittools_*.deb')"
SILEO_DEB="$(find_latest_deb 'org.coolstar.sileo_*.deb')"
if [[ -z "$UK_DEB" || -z "$SILEO_DEB" || ! -f "$PROCURSUS_DIR/bootstrap-iphoneos-arm64.tar.zst" ]]; then
  echo "[*] Fetching Sileo & uikittools packages from Procursus..."
  if ! "$PY_BIN" "$ROOT_DIR/scripts/resolve_procursus_debs.py" \
    --cache-dir "$PROCURSUS_DIR" \
    --packages uikittools org.coolstar.sileo; then
    if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
      report_sileo_fail "$LINENO" 1 "sileo_deb_fetch_failed" "Failed to download Sileo/uikittools packages from Procursus"
      exit 1
    fi
    echo "[!] Could not download Sileo packages (offline/network error) — skipped (non-fatal)." >&2
    exit 0
  fi
  UK_DEB="$(find_latest_deb 'uikittools_*.deb')"
  SILEO_DEB="$(find_latest_deb 'org.coolstar.sileo_*.deb')"
fi

if [[ -z "$UK_DEB" || -z "$SILEO_DEB" ]]; then
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    echo "[!] Could not locate Sileo or uikittools .deb in $PROCURSUS_DIR/debs." >&2
    report_sileo_fail "$LINENO" 1 "sileo_deb_missing" "Missing Sileo or uikittools .deb after fetch"
    exit 1
  fi
  echo "[!] Sileo or uikittools .deb unavailable — skipped (non-fatal)." >&2
  exit 0
fi

ensure_gmr_binaries() {
  local bin_dir="$BUILD_DIR/bin"
  local gmr_bin="$bin_dir/yigitzra1n_givemeroot"
  local sshd_bin="$bin_dir/ios16_sshd_launcher"
  local need_gmr=0 need_sshd=0

  if [[ ! -x "$gmr_bin" || "$ROOT_DIR/src/yigitzra1n_givemeroot.c" -nt "$gmr_bin" ]] || ! grep -qa "yigitzra1n_gmr" "$gmr_bin" 2>/dev/null; then
    need_gmr=1
  fi
  if [[ ! -x "$sshd_bin" || "$ROOT_DIR/src/ios16_sshd_launcher.c" -nt "$sshd_bin" ]] || ! grep -qa "yigitzra1n_gmr_daemon_v2" "$sshd_bin" 2>/dev/null; then
    need_sshd=1
  fi
  if [[ "$need_gmr" -eq 0 && "$need_sshd" -eq 0 ]]; then
    return 0
  fi

  local ios_sdk=""
  ios_sdk="$("$PY_BIN" "$ROOT_DIR/scripts/prepare_ios_sdk.py" "$ROOT_DIR" 2>/dev/null || true)"
  if [[ -z "$ios_sdk" ]] || ! command -v clang >/dev/null 2>&1; then
    echo "[!] Warning: could not locate iOS SDK/clang to compile Sileo root helper." >&2
    return 1
  fi
  mkdir -p "$bin_dir"
  if [[ "$need_gmr" -eq 1 ]]; then
    clang -isysroot "$ios_sdk" -target arm64-apple-ios15.0 -O2 -Wall -Wextra \
      "$ROOT_DIR/src/yigitzra1n_givemeroot.c" -o "$gmr_bin" || return 1
    codesign -f -s - --entitlements "$ROOT_DIR/entitlements/givemeroot.entitlements.plist" "$gmr_bin" >/dev/null || return 1
  fi
  if [[ "$need_sshd" -eq 1 ]]; then
    clang -isysroot "$ios_sdk" -target arm64-apple-ios15.0 -O2 -Wall -Wextra \
      "$ROOT_DIR/src/ios16_sshd_launcher.c" -o "$sshd_bin" || return 1
    codesign -f -s - --entitlements "$ROOT_DIR/entitlements/sshd_launcher.entitlements.plist" "$sshd_bin" >/dev/null || return 1
  fi
}

ensure_gmr_binaries || true

STAGE_APP="$WORK_TMP/stage_app"
STAGE_JB="$WORK_TMP/stage_jb"
mkdir -p "$STAGE_APP" "$STAGE_JB" "$WORK_TMP/raw_jb"

if ! "$PY_BIN" "$ROOT_DIR/scripts/stage_procursus_debs.py" \
  --stage-root "$WORK_TMP/raw_jb/var/jb" \
  --deb "$UK_DEB" \
  --deb "$SILEO_DEB" \
  --skip-users; then
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    report_sileo_fail "$LINENO" 1 "sileo_stage_failed" "stage_procursus_debs.py failed"
    exit 1
  fi
  echo "[!] Sileo package staging failed — skipped (non-fatal)." >&2
  exit 0
fi

if [[ -d "$WORK_TMP/raw_jb/var/jb/Applications/Sileo.app" ]]; then
  ditto "$WORK_TMP/raw_jb/var/jb/Applications/Sileo.app" "$STAGE_APP/Sileo.app"
  rm -rf "$WORK_TMP/raw_jb/var/jb/Applications/Sileo.app"
fi
if [[ -x "$BUILD_DIR/bin/yigitzra1n_givemeroot" && -d "$STAGE_APP/Sileo.app" ]]; then
  cp -f "$BUILD_DIR/bin/yigitzra1n_givemeroot" "$STAGE_APP/Sileo.app/giveMeRoot"
  chmod 755 "$STAGE_APP/Sileo.app/giveMeRoot"
fi

ditto "$WORK_TMP/raw_jb/var/jb" "$STAGE_JB"
mkdir -p "$STAGE_JB/Applications" "$STAGE_JB/tmp"
ln -sfn "/Applications/Sileo.app" "$STAGE_JB/Applications/Sileo.app"
if [[ -x "$BUILD_DIR/bin/ios16_sshd_launcher" ]]; then
  cp -f "$BUILD_DIR/bin/ios16_sshd_launcher" "$STAGE_JB/tmp/yigitzra1n_sshd_launcher"
  chmod 755 "$STAGE_JB/tmp/yigitzra1n_sshd_launcher"
fi

# Extract single-package status stanzas so we merge cleanly without overwriting existing dpkg/status.
if [[ -f "$STAGE_JB/Library/dpkg/status" ]]; then
  cp -f "$STAGE_JB/Library/dpkg/status" "$STAGE_JB/tmp/yigitzra1n_sileo.status_add"
  rm -f "$STAGE_JB/Library/dpkg/status"
fi

# Build yigitzra1n statusBarOverrides (3944 bytes) for legacy users too.
if [[ -f "$ROOT_DIR/scripts/build_statusbar_overrides.py" ]]; then
  "$PY_BIN" "$ROOT_DIR/scripts/build_statusbar_overrides.py" \
    --out "$STAGE_JB/tmp/yigitzra1n_statusBarOverrides" 2>/dev/null || true
fi

# Best-effort pre-seed APT lists (Havoc, Chariz, ElleKit, opa334, BigBoss, Procursus) so Sileo never fails with missing _Packages
"$PY_BIN" - "$PROCURSUS_DIR/apt_lists" "$STAGE_JB/tmp/apt_lists_seed" <<'PY' 2>/dev/null || true
import bz2, gzip, lzma, pathlib, shutil, subprocess, sys, time

cache_dir = pathlib.Path(sys.argv[1])
out_dir = pathlib.Path(sys.argv[2])
cache_dir.mkdir(parents=True, exist_ok=True)
out_dir.mkdir(parents=True, exist_ok=True)

MAX_AGE = 6 * 3600
now = time.time()
UA = "Telesphoreo APT-HTTP/1.0.592"

def fetch_bytes(url, timeout=6):
    try:
        p = subprocess.run(
            ["curl", "-fsSL", "--connect-timeout", "3", "--max-time", str(timeout), "-A", UA, url],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 2,
        )
        if p.returncode == 0 and p.stdout:
            return p.stdout
    except Exception:
        pass
    return None

def decompress(raw, ext):
    try:
        if ext == "":
            return raw
        if ext == ".bz2":
            return bz2.decompress(raw)
        if ext == ".gz":
            return gzip.decompress(raw)
        if ext == ".xz":
            return lzma.decompress(raw)
        if ext == ".zst":
            p = subprocess.run(["zstd", "-dc"], input=raw, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            return p.stdout if p.returncode == 0 and p.stdout else None
    except Exception:
        return None
    return None

def ensure_file(name, candidates, timeout=6):
    cp = cache_dir / name
    if cp.is_file() and cp.stat().st_size > 0 and (now - cp.stat().st_mtime) < MAX_AGE:
        shutil.copy2(cp, out_dir / name)
        return
    for url, ext in candidates:
        raw = fetch_bytes(url, timeout=timeout)
        if not raw:
            continue
        data = decompress(raw, ext)
        if data and len(data) > 0:
            tmp = cp.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(cp)
            shutil.copy2(cp, out_dir / name)
            return
    if cp.is_file() and cp.stat().st_size > 0:
        shutil.copy2(cp, out_dir / name)

PKG_EXTS = [".zst", ".xz", ".bz2", ".gz", ""]
ensure_file("havoc.app_._Release", [("https://havoc.app/Release", "")])
ensure_file("havoc.app_._Release.gpg", [("https://havoc.app/Release.gpg", "")])
ensure_file("havoc.app_._Packages", [("https://havoc.app/Packages" + e, e) for e in PKG_EXTS], timeout=10)

ensure_file("repo.chariz.com_._Release", [("https://repo.chariz.com/Release", "")])
ensure_file("repo.chariz.com_._Release.gpg", [("https://repo.chariz.com/Release.gpg", "")])
ensure_file("repo.chariz.com_._Packages", [("https://repo.chariz.com/Packages" + e, e) for e in PKG_EXTS], timeout=10)

ensure_file("ellekit.space_._Release", [("https://ellekit.space/Release", "")])
ensure_file("ellekit.space_._Packages", [("https://ellekit.space/Packages" + e, e) for e in PKG_EXTS])

ensure_file("opa334.github.io_._Release", [("https://opa334.github.io/Release", "")])
ensure_file("opa334.github.io_._Packages", [("https://opa334.github.io/Packages" + e, e) for e in PKG_EXTS])

ensure_file("apt.thebigboss.org_repofiles_cydia_dists_stable_Release", [("http://apt.thebigboss.org/repofiles/cydia/dists/stable/Release", "")])
ensure_file("apt.thebigboss.org_repofiles_cydia_dists_stable_Release.gpg", [("http://apt.thebigboss.org/repofiles/cydia/dists/stable/Release.gpg", "")])
ensure_file(
    "apt.thebigboss.org_repofiles_cydia_dists_stable_main_binary-iphoneos-arm64_Packages",
    [("http://apt.thebigboss.org/repofiles/cydia/dists/stable/main/binary-iphoneos-arm64/Packages" + e, e) for e in [".bz2", ".gz", ""]],
)

ensure_file("apt.procurs.us_dists_1900_InRelease", [("https://apt.procurs.us/dists/1900/InRelease", "")])
ensure_file(
    "apt.procurs.us_dists_1900_main_binary-iphoneos-arm64_Packages",
    [("https://apt.procurs.us/dists/1900/main/binary-iphoneos-arm64/Packages" + e, e) for e in PKG_EXTS],
    timeout=10,
)
PY

echo "[*] Installing Sileo & default repos on device..."
SSH_ERR_LOG="$WORK_TMP/ssh_err.log"
ssh_rc=0
( COPYFILE_DISABLE=1 tar -C "$STAGE_APP" --no-xattrs -cf - Sileo.app | run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  /sbin/mount -uw / 2>/dev/null || true
  rm -rf /Applications/Sileo.app
  mkdir -p /Applications
  tar --no-same-owner -m -C /Applications -xf -
  chown -R root:wheel /Applications/Sileo.app
  chmod 755 /Applications/Sileo.app /Applications/Sileo.app/Sileo
  if [ -f /Applications/Sileo.app/giveMeRoot ]; then
    chown root:wheel /Applications/Sileo.app/giveMeRoot
    chmod 4755 /Applications/Sileo.app/giveMeRoot
  fi
' ) 2>"$SSH_ERR_LOG" || ssh_rc=$?
if [[ "$ssh_rc" -ne 0 ]]; then
  cat "$SSH_ERR_LOG" >&2 || true
  err_tail="$(tr '\n' ' ' < "$SSH_ERR_LOG" | tail -c 400 || true)"
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    report_sileo_fail "$LINENO" "$ssh_rc" "sileo_app_transfer_failed" "${err_tail:-Sileo.app SSH transfer failed (rc=$ssh_rc)}"
    exit "$ssh_rc"
  fi
  echo "[!] Sileo transfer failed — skipped (non-fatal)." >&2
  exit 0
fi

ssh_rc=0
( COPYFILE_DISABLE=1 tar -C "$STAGE_JB" --no-xattrs -cf - . | run_ssh '
  set -e
  export PATH="/var/jb/usr/local/sbin:/var/jb/usr/local/bin:/var/jb/usr/sbin:/var/jb/usr/bin:/var/jb/sbin:/var/jb/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  need_sbreload=0
  [ ! -f /private/var/jb/.yigitzra1n_sileo_ready ] && need_sbreload=1

  if [ -d /private/var/jb/Applications/Sileo.app ] && [ ! -L /private/var/jb/Applications/Sileo.app ]; then
    rm -rf /private/var/jb/Applications/Sileo.app
  fi
  mkdir -p /private/var/jb/Applications /private/var/jb/Library/dpkg/info /private/var/jb/tmp /private/var/jb/etc/apt/sources.list.d
  tar --no-same-owner -m -C /private/var/jb -xf -
  chown -R root:wheel /private/var/jb/Applications/Sileo.app /private/var/jb/Library/dpkg/info 2>/dev/null || true

  for tool in uicache sbreload; do
    [ -x "/var/jb/usr/bin/$tool" ] && ln -sf "/var/jb/usr/bin/$tool" "/usr/bin/$tool" || true
  done

  : > /private/var/jb/.procursus_strapped
  mkdir -p /private/var/jb/var/mobile/Library/Caches /private/var/jb/var/mobile/Library/Preferences
  chown -R mobile:mobile /private/var/jb/var/mobile
  chmod 755 /private/var/jb/var/mobile /private/var/jb/var/mobile/Library /private/var/jb/var/mobile/Library/Caches /private/var/jb/var/mobile/Library/Preferences
  if [ -x /var/jb/usr/libexec/firmware ]; then
    /var/jb/usr/libexec/firmware >/dev/null 2>&1 || true
    : > /private/var/jb/.yigitzra1n_fw_ready
  fi

  STATUS_FILE="/private/var/jb/Library/dpkg/status"
  ADD_FILE="/private/var/jb/tmp/yigitzra1n_sileo.status_add"
  if [ -f "$ADD_FILE" ]; then
    touch "$STATUS_FILE"
    : > "${STATUS_FILE}.tmp"
    blk=""
    skip=0
    while IFS= read -r ln || [ -n "$ln" ]; do
      if [ -z "$ln" ]; then
        if [ -n "$blk" ] && [ "$skip" -eq 0 ]; then
          printf "%s\n\n" "$blk" >> "${STATUS_FILE}.tmp"
        fi
        blk=""
        skip=0
      else
        case "$ln" in
          "Package: uikittools"|"Package: org.coolstar.sileo")
            skip=1
            ;;
        esac
        if [ -z "$blk" ]; then
          blk="$ln"
        else
          blk="${blk}
${ln}"
        fi
      fi
    done < "$STATUS_FILE"
    if [ -n "$blk" ] && [ "$skip" -eq 0 ]; then
      printf "%s\n\n" "$blk" >> "${STATUS_FILE}.tmp"
    fi
    cat "$ADD_FILE" >> "${STATUS_FILE}.tmp"
    printf "\n" >> "${STATUS_FILE}.tmp"
    mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
    rm -f "$ADD_FILE"
  fi

  # Seed/upgrade default rootless repositories (adds Trusted: yes for key-less repos once)
  SOURCES_FILE="/private/var/jb/etc/apt/sources.list.d/sileo.sources"
  if [ ! -f /private/var/jb/.yigitzra1n_repos_v2_ready ]; then
    touch "$SOURCES_FILE"
    # If prior un-trusted v1 stanzas exist for ellekit/opa334/bigboss, rewrite clean default block while keeping any extra custom repos
    if [ -f /private/var/jb/.yigitzra1n_repos_ready ]; then
      : > "${SOURCES_FILE}.new"
      cur_blk=""
      drop=0
      while IFS= read -r ln || [ -n "$ln" ]; do
        if [ -z "$ln" ]; then
          if [ -n "$cur_blk" ] && [ "$drop" -eq 0 ]; then
            printf "%s\n\n" "$cur_blk" >> "${SOURCES_FILE}.new"
          fi
          cur_blk=""
          drop=0
        else
          case "$ln" in
            *havoc.app*|*repo.chariz.com*|*ellekit.space*|*opa334.github.io*|*apt.thebigboss.org*)
              drop=1
              ;;
          esac
          if [ -z "$cur_blk" ]; then
            cur_blk="$ln"
          else
            cur_blk="${cur_blk}
${ln}"
          fi
        fi
      done < "$SOURCES_FILE"
      if [ -n "$cur_blk" ] && [ "$drop" -eq 0 ]; then
        printf "%s\n\n" "$cur_blk" >> "${SOURCES_FILE}.new"
      fi
      mv "${SOURCES_FILE}.new" "$SOURCES_FILE"
    fi
    append_repo_stanza() {
      repo_uri="$1"
      repo_suite="$2"
      repo_comp="$3"
      repo_trusted="$4"
      if ! grep -qF "$repo_uri" "$SOURCES_FILE" 2>/dev/null; then
        [ -s "$SOURCES_FILE" ] && printf "\n" >> "$SOURCES_FILE"
        printf "Types: deb\nURIs: %s\nSuites: %s\nComponents:%s\n" "$repo_uri" "$repo_suite" "$repo_comp" >> "$SOURCES_FILE"
        [ "$repo_trusted" = "1" ] && printf "Trusted: yes\n" >> "$SOURCES_FILE"
      fi
    }
    append_repo_stanza "https://havoc.app/" "./" "" 0
    append_repo_stanza "https://repo.chariz.com/" "./" "" 0
    append_repo_stanza "https://ellekit.space/" "./" "" 1
    append_repo_stanza "https://opa334.github.io/" "./" "" 1
    append_repo_stanza "http://apt.thebigboss.org/repofiles/cydia/" "stable" " main" 1
    chown root:wheel "$SOURCES_FILE"
    chmod 644 "$SOURCES_FILE"
    : > /private/var/jb/.yigitzra1n_repos_ready
    : > /private/var/jb/.yigitzra1n_repos_v2_ready
  fi

  # Seed APT lists & sileolists (mobile-writable mirror) so Sileo never fails with missing _Packages
  mkdir -p /private/var/jb/var/lib/apt/lists/partial /private/var/jb/var/lib/apt/sileolists/partial
  SEED_LISTS="/private/var/jb/tmp/apt_lists_seed"
  if [ -d "$SEED_LISTS" ]; then
    for sf in "$SEED_LISTS"/*; do
      [ -f "$sf" ] || continue
      bn="$(basename "$sf")"
      if [ ! -s "/private/var/jb/var/lib/apt/lists/$bn" ]; then
        cp -f "$sf" "/private/var/jb/var/lib/apt/lists/$bn"
      fi
    done
    rm -rf "$SEED_LISTS"
  fi
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -o Acquire::http::Timeout=5 -o Acquire::https::Timeout=5 -o Acquire::Retries=1 >/dev/null 2>&1 || true
  fi
  for lf in /private/var/jb/var/lib/apt/lists/*; do
    [ -f "$lf" ] || continue
    case "$(basename "$lf")" in
      lock) continue ;;
    esac
    chmod 644 "$lf" 2>/dev/null || true
    cp -f "$lf" "/private/var/jb/var/lib/apt/sileolists/$(basename "$lf")" 2>/dev/null || true
  done
  chown -R mobile:mobile /private/var/jb/var/lib/apt/sileolists 2>/dev/null || chown -R 501:501 /private/var/jb/var/lib/apt/sileolists 2>/dev/null || true
  chmod 755 /private/var/jb/var/lib/apt/lists /private/var/jb/var/lib/apt/sileolists /private/var/jb/var/lib/apt/sileolists/partial 2>/dev/null || true

  # Update ios16_sshd_launcher & start yigitzra1n_gmr root helper daemon if needed
  SSH_L_STAGE="/private/var/jb/tmp/yigitzra1n_sshd_launcher"
  if [ -f "$SSH_L_STAGE" ]; then
    /sbin/mount -uw / 2>/dev/null || true
    mkdir -p /usr/local/libexec
    cp -f "$SSH_L_STAGE" /usr/local/libexec/ios16_sshd_launcher.new
    chown root:wheel /usr/local/libexec/ios16_sshd_launcher.new
    chmod 755 /usr/local/libexec/ios16_sshd_launcher.new
    mv -f /usr/local/libexec/ios16_sshd_launcher.new /usr/local/libexec/ios16_sshd_launcher
    rm -f "$SSH_L_STAGE"
    /usr/local/libexec/ios16_sshd_launcher --gmr-daemon || true
  fi

  # Install yigitzra1n carrier statusBarOverrides for legacy installs if missing/corrupt
  SB_STAGE="/private/var/jb/tmp/yigitzra1n_statusBarOverrides"
  if [ -f "$SB_STAGE" ] && [ "$(wc -c < "$SB_STAGE" 2>/dev/null | tr -d " ")" = "3944" ]; then
    mkdir -p /usr/local/share/yigitzra1n
    if [ ! -f /usr/local/share/yigitzra1n/statusBarOverrides ] || [ "$(wc -c < /usr/local/share/yigitzra1n/statusBarOverrides 2>/dev/null | tr -d " ")" != "3944" ]; then
      cp -f "$SB_STAGE" /usr/local/share/yigitzra1n/statusBarOverrides
      chown root:wheel /usr/local/share/yigitzra1n/statusBarOverrides
      chmod 644 /usr/local/share/yigitzra1n/statusBarOverrides
    fi
    mkdir -p /private/var/mobile/Library/SpringBoard
    chown mobile:mobile /private/var/mobile/Library /private/var/mobile/Library/SpringBoard 2>/dev/null || true
    if [ ! -f /private/var/mobile/Library/SpringBoard/statusBarOverrides ] || [ "$(wc -c < /private/var/mobile/Library/SpringBoard/statusBarOverrides 2>/dev/null | tr -d " ")" != "3944" ]; then
      cp -f "$SB_STAGE" /private/var/mobile/Library/SpringBoard/statusBarOverrides
      chown mobile:mobile /private/var/mobile/Library/SpringBoard/statusBarOverrides
      chmod 644 /private/var/mobile/Library/SpringBoard/statusBarOverrides
      need_sbreload=1
    fi
  fi
  rm -f "$SB_STAGE"

  /var/jb/usr/bin/uicache -u /Applications/Sileo.app >/dev/null 2>&1 || true
  for _u in 1 2; do
    if /var/jb/usr/bin/uicache -p /Applications/Sileo.app >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  : > /private/var/jb/.yigitzra1n_sileo_ready
  killall -9 Sileo >/dev/null 2>&1 || true
  if [ "$need_sbreload" -eq 1 ] && [ -x /var/jb/usr/bin/sbreload ]; then
    /var/jb/usr/bin/sbreload >/dev/null 2>&1 || true
  fi
  sync
' ) 2>"$SSH_ERR_LOG" || ssh_rc=$?
if [[ "$ssh_rc" -ne 0 ]]; then
  cat "$SSH_ERR_LOG" >&2 || true
  err_tail="$(tr '\n' ' ' < "$SSH_ERR_LOG" | tail -c 400 || true)"
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    report_sileo_fail "$LINENO" "$ssh_rc" "sileo_jb_provision_failed" "${err_tail:-Sileo SSH provision failed (rc=$ssh_rc)}"
    exit "$ssh_rc"
  fi
  echo "[!] Sileo device setup failed — skipped (non-fatal)." >&2
  exit 0
fi
echo "[+] Sileo & default repos ready on device."
