#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/config/firmware_urls.env"
source "$ROOT_DIR/scripts/telemetry.sh"
export TELEMETRY_MARKER_FILE="${TELEMETRY_MARKER_FILE:-$ROOT_DIR/build/.tmp/.telemetry_reported}"
[[ "${TELEMETRY_PARENT:-0}" == "1" ]] || rm -f "$TELEMETRY_MARKER_FILE" 2>/dev/null || true

FETCH_STEP="FETCH_INIT"
trap 'telemetry_trap_err "${TELEMETRY_CMD:-prepare}" "$FETCH_STEP" "01_fetch_assets.sh" "$LINENO" "$?" "${BASH_COMMAND:-}"' ERR

IPSW_DIR="${IPSW_DIR:-$ROOT_DIR/ipsw}"
CACHE_DIR="$ROOT_DIR/.cache"
ENGINE_DIR="$CACHE_DIR/engine"
PROCURSUS_DIR="$CACHE_DIR/procursus"
SKIP_DOWNLOAD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ipsw-dir) IPSW_DIR="$2"; shift 2 ;;
    --skip-download) SKIP_DOWNLOAD=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

mkdir -p "$IPSW_DIR" "$ENGINE_DIR" "$PROCURSUS_DIR"

fetch_with_resume() {
  local url="$1"
  local part="$2"
  local label="$3"
  local attempt=1
  local rc=0

  while (( attempt <= 3 )); do
    set +e
    curl -fL -C - --connect-timeout 15 --retry 5 --retry-delay 3 --progress-bar -o "$part" "$url"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      return 0
    fi
    if [[ "$rc" -eq 33 ]]; then
      rm -f "$part"
    fi
    if (( attempt < 3 )); then
      echo "[!] $label download interrupted (curl exit $rc). Resuming in 4s (attempt $((attempt + 1))/3)..." >&2
      sleep 4
    fi
    attempt=$((attempt + 1))
  done
  return "$rc"
}

verify_local_ipsw() {
  local path="$1"
  local label="$2"
  local rc=0
  python3 "$ROOT_DIR/scripts/verify_ipsw.py" "$path" "$label IPSW" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    local reason="invalid_ipsw_zip"
    [[ "$rc" -eq 21 ]] && reason="invalid_ipsw_alias"
    telemetry_send "install_fail" "${TELEMETRY_CMD:-prepare}" "$FETCH_STEP" "01_fetch_assets.sh" "$LINENO" "$rc" "$reason" "$label IPSW invalid: $path"
    telemetry_mark_reported
    exit 1
  fi
}

resolve_or_fetch_ipsw() {
  local label="$1"
  local glob_pat="$2"
  local default_name="$3"
  local url="$4"

  local found=""
  found="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$glob_pat" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$found" ]]; then
    verify_local_ipsw "$found" "$label"
    echo "[+] Found local $label IPSW: $found"
    return 0
  fi

  if [[ "$SKIP_DOWNLOAD" -eq 1 ]]; then
    echo "[ERROR] Missing $label IPSW in $IPSW_DIR (pattern: $glob_pat) and --skip-download is set." >&2
    echo "        Place $default_name into $IPSW_DIR or run without --skip-download." >&2
    exit 1
  fi

  local dst="$IPSW_DIR/$default_name"
  local part="${dst}.part"
  echo "[*] Downloading $label IPSW from Apple CDN..."
  echo "    URL : $url"
  echo "    Dest: $dst"
  fetch_with_resume "$url" "$part" "$label IPSW"
  mv "$part" "$dst"
  verify_local_ipsw "$dst" "$label"
  echo "[+] Downloaded $label IPSW: $dst"
}

echo "=== [1/3] Checking / Downloading Stock Apple IPSWs ==="
FETCH_STEP="FETCH_IPSW_6S"
resolve_or_fetch_ipsw "iPhone 6s (15.8.8)" "$IPSW_6S_1588_GLOB" "$IPSW_6S_1588_NAME" "$IPSW_6S_1588_URL"
FETCH_STEP="FETCH_IPSW_IPAD5"
resolve_or_fetch_ipsw "iPad 5 (16.7.16)" "$IPSW_IPAD5_16716_GLOB" "$IPSW_IPAD5_16716_NAME" "$IPSW_IPAD5_16716_URL"
FETCH_STEP="FETCH_IPSW_IP8"
resolve_or_fetch_ipsw "iPhone 8 (16.7.16)" "$IPSW_IP8_16716_GLOB" "$IPSW_IP8_16716_NAME" "$IPSW_IP8_16716_URL"

echo "=== [2/3] Checking / Downloading Upstream A9 Boot & Restore Runtime ==="
FETCH_STEP="FETCH_ENGINE"
if ! find "$ENGINE_DIR/raw" -type f -name "turdus_merula" 2>/dev/null | grep -q .; then
  ARCHIVE="$ENGINE_DIR/$ENGINE_ARCHIVE_NAME"
  if [[ ! -f "$ARCHIVE" ]]; then
    if [[ "$SKIP_DOWNLOAD" -eq 1 ]]; then
      echo "[ERROR] Upstream runtime archive missing at $ARCHIVE and --skip-download is set." >&2
      exit 1
    fi
    echo "[*] Downloading upstream A9 boot/restore runtime..."
    fetch_with_resume "$ENGINE_URL" "${ARCHIVE}.part" "Upstream A9 runtime"
    mv "${ARCHIVE}.part" "$ARCHIVE"
  fi
  rm -rf "$ENGINE_DIR/raw"
  mkdir -p "$ENGINE_DIR/raw"
  tar -xzf "$ARCHIVE" -C "$ENGINE_DIR/raw"
  /usr/bin/xattr -cr "$ENGINE_DIR/raw" 2>/dev/null || true
  find "$ENGINE_DIR/raw" -type f \( -name "turdus_merula" -o -name "turdusra1n" \) -exec chmod +x {} +
  echo "[+] Upstream runtime ready in $ENGINE_DIR/raw"
else
  echo "[+] Upstream runtime already cached in $ENGINE_DIR/raw"
fi

echo "=== [3/3] Checking / Downloading Open-Source Procursus SSH/CLI Bootstrap ==="
FETCH_STEP="FETCH_PROCURSUS"
VENV_PY="$ROOT_DIR/.venv/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="python3"
if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  mkdir -p "$ROOT_DIR/build/.tmp"
  export ROOT_DIR VENV_PY
  export TELEMETRY_CMD="${TELEMETRY_CMD:-prepare}"
  export TELEMETRY_STEP="$FETCH_STEP"
  export TELEMETRY_MARKER_FILE="${TELEMETRY_MARKER_FILE:-$ROOT_DIR/build/.tmp/.telemetry_reported}"
  telemetry_run_py "resolve_procursus_debs.py" \
    --cache-dir "$PROCURSUS_DIR" \
    --packages openssh-server openssh-sftp-server openssh-client bash coreutils gzip zstd tar findutils grep sed curl nano lsof less
else
  echo "[*] Skipping Procursus fetch (--skip-download)."
fi

echo "[+] All required assets ready."
