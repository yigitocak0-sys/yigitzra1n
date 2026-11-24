#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT_DIR/config/firmware_urls.env"

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

resolve_or_fetch_ipsw() {
  local label="$1"
  local glob_pat="$2"
  local default_name="$3"
  local url="$4"

  local found=""
  found="$(find -L "$IPSW_DIR" -maxdepth 2 -type f -name "$glob_pat" ! -name "*.part" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$found" ]]; then
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
  curl -fL -C - --retry 5 --retry-delay 3 --progress-bar -o "$part" "$url"
  mv "$part" "$dst"
  echo "[+] Downloaded $label IPSW: $dst"
}

echo "=== [1/3] Checking / Downloading Stock Apple IPSWs ==="
resolve_or_fetch_ipsw "iPhone 6s (15.8.8)" "$IPSW_6S_1588_GLOB" "$IPSW_6S_1588_NAME" "$IPSW_6S_1588_URL"
resolve_or_fetch_ipsw "iPad 5 (16.7.16)" "$IPSW_IPAD5_16716_GLOB" "$IPSW_IPAD5_16716_NAME" "$IPSW_IPAD5_16716_URL"
resolve_or_fetch_ipsw "iPhone 8 (16.7.16)" "$IPSW_IP8_16716_GLOB" "$IPSW_IP8_16716_NAME" "$IPSW_IP8_16716_URL"

echo "=== [2/3] Checking / Downloading Upstream A9 Boot & Restore Runtime ==="
if ! find "$ENGINE_DIR/raw" -type f -name "turdus_merula" 2>/dev/null | grep -q .; then
  ARCHIVE="$ENGINE_DIR/$ENGINE_ARCHIVE_NAME"
  if [[ ! -f "$ARCHIVE" ]]; then
    if [[ "$SKIP_DOWNLOAD" -eq 1 ]]; then
      echo "[ERROR] Upstream runtime archive missing at $ARCHIVE and --skip-download is set." >&2
      exit 1
    fi
    echo "[*] Downloading upstream A9 boot/restore runtime..."
    curl -fL -C - --retry 3 --progress-bar -o "${ARCHIVE}.part" "$ENGINE_URL"
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
VENV_PY="$ROOT_DIR/.venv/bin/python3"
[[ -x "$VENV_PY" ]] || VENV_PY="python3"
if [[ "$SKIP_DOWNLOAD" -eq 0 ]]; then
  "$VENV_PY" "$ROOT_DIR/scripts/resolve_procursus_debs.py" \
    --cache-dir "$PROCURSUS_DIR" \
    --packages openssh-server openssh-sftp-server openssh-client bash coreutils gzip zstd tar findutils grep sed curl nano lsof less
else
  echo "[*] Skipping Procursus fetch (--skip-download)."
fi

echo "[+] All required assets ready."
