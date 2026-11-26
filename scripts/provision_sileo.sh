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

for req in sshpass ssh lsof tar codesign; do
  if ! command -v "$req" >/dev/null 2>&1; then
    if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
      echo "[ERROR] Missing required host tool: '$req'." >&2
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

echo "[*] Waiting for iOS SSH to check/install Sileo, repos & status bar (Ctrl+C to skip)..."
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
  grep -q "^Package: org.coolstar.sileo$" /var/jb/Library/dpkg/status 2>/dev/null
' >/dev/null 2>&1; then
  already_ok=1
fi

if [[ "$already_ok" -eq 1 ]]; then
  echo "[+] Sileo, default repos & yigitzra1n status bar already installed."
  exit 0
fi

if ! run_ssh '[ -L /var ] || [ ! -e /var ]'; then
  if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
    echo "[!] Device /var is not a symlink (expected /var -> private/var). Repair /var via ssh-ramdisk before provisioning Sileo." >&2
    exit 1
  fi
  echo "[!] Device /var check failed — Sileo setup skipped (non-fatal)." >&2
  exit 0
fi

find_latest_deb() {
  local pat="$1"
  find "$PROCURSUS_DIR/debs" -maxdepth 1 -type f -name "$pat" 2>/dev/null | sort -V | tail -n 1 || true
}

UK_DEB="$(find_latest_deb 'uikittools_*.deb')"
SILEO_DEB="$(find_latest_deb 'org.coolstar.sileo_*.deb')"
if [[ -z "$UK_DEB" || -z "$SILEO_DEB" || ! -f "$PROCURSUS_DIR/bootstrap-iphoneos-arm64.tar.zst" ]]; then
  echo "[*] Fetching Sileo & uikittools packages from Procursus..."
  if ! "$PY_BIN" "$ROOT_DIR/scripts/resolve_procursus_debs.py" \
    --cache-dir "$PROCURSUS_DIR" \
    --packages uikittools org.coolstar.sileo; then
    if [[ "$SILEO_REQUIRE_SSH" -eq 1 ]]; then
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

echo "[*] Installing Sileo, default repos & yigitzra1n status bar on device..."
COPYFILE_DISABLE=1 tar -C "$STAGE_APP" --no-xattrs -cf - Sileo.app | run_ssh '
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
'

COPYFILE_DISABLE=1 tar -C "$STAGE_JB" --no-xattrs -cf - . | run_ssh '
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
'
echo "[+] Sileo, default repos & yigitzra1n status bar ready on device."
