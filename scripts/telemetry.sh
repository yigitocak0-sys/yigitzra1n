#!/usr/bin/env bash

YIGITZRA1N_TELEMETRY_URL="${YIGITZRA1N_TELEMETRY_URL:-https://telemetry.yigitz.com/v1/event}"
YIGITZRA1N_MESSAGE_URL="${YIGITZRA1N_MESSAGE_URL:-https://telemetry.yigitz.com/v1/message}"
YIGITZRA1N_MESSAGES_URL="${YIGITZRA1N_MESSAGES_URL:-https://telemetry.yigitz.com/v1/messages}"
YIGITZRA1N_NO_TELEMETRY="${YIGITZRA1N_NO_TELEMETRY:-0}"
if [[ -z "${YIGITZRA1N_TOOL_VERSION:-}" ]]; then
  _ver_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/VERSION"
  if [[ -f "$_ver_file" ]]; then
    YIGITZRA1N_TOOL_VERSION="$(tr -d '[:space:]' < "$_ver_file")"
  else
    YIGITZRA1N_TOOL_VERSION="1.19"
  fi
fi

telemetry_mark_reported() {
  if [[ -n "${TELEMETRY_MARKER_FILE:-}" ]]; then
    mkdir -p "$(dirname "$TELEMETRY_MARKER_FILE")" 2>/dev/null || true
    : > "$TELEMETRY_MARKER_FILE" 2>/dev/null || true
  fi
}

telemetry_was_reported() {
  [[ -n "${TELEMETRY_MARKER_FILE:-}" && -f "$TELEMETRY_MARKER_FILE" ]]
}

telemetry_print_contact_note() {
  echo "" >&2
  echo "[!] If the script failed or you need help, please contact me:" >&2
  echo "    Instagram  : @ygtxzra1n" >&2
  echo "    X (Twitter): @springboard_app" >&2
}

telemetry_effective_version() {
  local ver="$YIGITZRA1N_TOOL_VERSION"
  case "${YIGITZRA1N_DEVICE_SOC:-}" in
    samsung_0x8000_forced) ver="${ver}+s8000" ;;
    unexpected_forced) ver="${ver}+forced" ;;
  esac
  printf '%s' "$ver"
}

telemetry_send() {
  local event="${1:-install_fail}"
  local command_name="${2:-unknown}"
  local step="${3:-}"
  local source_file="${4:-}"
  local line="${5:-0}"
  local exit_code="${6:-0}"
  local reason="${7:-}"
  local error_msg="${8:-}"
  local silent="${9:-0}"

  if [[ "$silent" != "1" ]] && [[ "$event" == "install_fail" || "$event" == "security_block" ]]; then
    if [[ "$reason" != "user_aborted" ]]; then
      telemetry_print_contact_note
    fi
  fi

  [[ "${YIGITZRA1N_NO_TELEMETRY:-0}" == "1" ]] && return 0
  command -v curl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local host_os=""
  local host_arch=""
  local tool_ver=""
  host_os="$(sw_vers -productVersion 2>/dev/null || uname -s 2>/dev/null || true)"
  host_arch="$(uname -m 2>/dev/null || true)"
  tool_ver="$(telemetry_effective_version)"

  local payload=""
  payload="$(python3 - "$event" "$command_name" "$step" "$source_file" "$line" "$exit_code" "$reason" "$error_msg" "$host_arch" "$host_os" "$tool_ver" <<'PY' 2>/dev/null || true
import hashlib, json, re, subprocess, sys

DOMAIN_SALT = b"yigitzra1n:one-way-anon-client:v1"
PBKDF2_ROUNDS = 200_000
TRUNC_HEX_LEN = 16

def derive_irreversible_client_id() -> str:
    try:
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception:
        return ""
    m = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', out)
    if not m:
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
    if not m:
        return ""
    raw_hw = m.group(1).strip()
    if not raw_hw:
        return ""
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        raw_hw.encode("utf-8"),
        DOMAIN_SALT,
        PBKDF2_ROUNDS,
    ).hex()[:TRUNC_HEX_LEN]
    del raw_hw
    return digest

def clean(s, n):
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).strip()
    s = re.sub(r"/(?:Users|home)/[^/\s:\"']+", "/<user>", s)
    s = re.sub(r"(?i)(\b(?:device\s+)?serial(?:\s+number)?(?:\s+is)?\s*[:= ]\s*|\bSRNM\s*:\s*\[?|\bSerialNumber\s*[:= ]\s*)[A-Za-z0-9]{6,20}", r"\1<serial>", s)
    s = re.sub(r"(?i)(\b(?:UniqueDeviceID|UDID)\s*[:= ]\s*)[0-9A-Fa-f-]{16,45}", r"\1<udid>", s)
    s = re.sub(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b", "<udid>", s)
    s = re.sub(r"\b[0-9a-fA-F]{40}\b", "<udid>", s)
    s = re.sub(r"(?i)(\b(?:ECID|UniqueChipID)\s*[:= ]\s*)(?:0x)?[0-9a-fA-F]{8,20}\b", r"\1<id>", s)
    s = re.sub(r"(?i)(\bIMEI2?\s*[:= ]\s*)\d{10,20}\b", r"\1<imei>", s)
    s = re.sub(r"\b\d{12,20}\b", "<id>", s)
    return s[:n]

try:
    line_num = int(sys.argv[5])
except ValueError:
    line_num = 0
try:
    rc = int(sys.argv[6])
except ValueError:
    rc = 0

print(json.dumps({
    "client_id": derive_irreversible_client_id(),
    "event": clean(sys.argv[1], 32),
    "command": clean(sys.argv[2], 24),
    "step": clean(sys.argv[3], 48),
    "source_file": clean(sys.argv[4], 64),
    "line": line_num,
    "exit_code": rc,
    "reason": clean(sys.argv[7], 64),
    "error_msg": clean(sys.argv[8], 1200),
    "host_arch": clean(sys.argv[9], 24),
    "host_os": clean(sys.argv[10], 32),
    "tool_version": clean(sys.argv[11], 32),
}))
PY
)"
  [[ -n "$payload" ]] || return 0

  curl -fsS --connect-timeout 2 --max-time 2 \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$YIGITZRA1N_TELEMETRY_URL" >/dev/null 2>&1 || true
}

telemetry_track_first_run() {
  local root_dir="${1:-}"
  local cmd_name="${2:-unknown}"
  [[ -n "$root_dir" && "$cmd_name" != "clean" ]] || return 0

  local flag_file="$root_dir/.yigitzra1n_init"
  [[ -f "$flag_file" ]] && return 0

  : > "$flag_file" 2>/dev/null || true
  telemetry_send "first_run" "$cmd_name" "FIRST_RUN" "yigitzra1n.sh" 0 0 "started" ""
}

telemetry_trap_err() {
  local command_name="${1:-unknown}"
  local step="${2:-}"
  local source_file="${3:-}"
  local line="${4:-0}"
  local exit_code="${5:-1}"
  local failed_cmd="${6:-}"

  [[ "$exit_code" -eq 130 ]] && return 0
  telemetry_was_reported && return 0
  telemetry_mark_reported
  telemetry_send "install_fail" "$command_name" "$step" "$source_file" "$line" "$exit_code" "command_failed" "$failed_cmd"
}

telemetry_on_exit() {
  local command_name="${1:-unknown}"
  local step="${2:-}"
  local exit_code="${3:-0}"

  if [[ "$exit_code" -eq 130 ]] && ! telemetry_was_reported; then
    telemetry_mark_reported
    telemetry_send "install_fail" "$command_name" "$step" "yigitzra1n.sh" 0 130 "user_aborted" "Aborted by user at $step"
  fi
}

telemetry_run_py() {
  local script_name="$1"
  shift
  local script_path="$ROOT_DIR/scripts/$script_name"
  local py_bin="${VENV_PY:-python3}"
  local err_file="${TMPDIR:-/tmp}/.py_err_$$"
  local rc=0

  if python3 - "$py_bin" "$script_path" "$err_file" "$@" <<'PY'; then
import subprocess, sys

py_bin, script_path, err_file = sys.argv[1], sys.argv[2], sys.argv[3]
args = sys.argv[4:]

proc = subprocess.Popen(
    [py_bin, script_path, *args],
    stderr=subprocess.PIPE,
)
tail_lines = []
assert proc.stderr is not None
while True:
    chunk = proc.stderr.readline()
    if not chunk:
        break
    sys.stderr.buffer.write(chunk)
    sys.stderr.buffer.flush()
    txt = chunk.decode("utf-8", errors="replace").strip()
    if txt:
        tail_lines.append(txt)
        if len(tail_lines) > 3:
            tail_lines.pop(0)

rc = proc.wait()
if rc < 0:
    rc = 130 if rc == -2 else (128 - rc)
with open(err_file, "w", encoding="utf-8") as f:
    if tail_lines:
        f.write(" | ".join(tail_lines))
sys.exit(rc)
PY
    rc=0
  else
    rc=$?
  fi

  local err_msg=""
  if [[ -f "$err_file" ]]; then
    err_msg="$(cat "$err_file" 2>/dev/null || true)"
    rm -f "$err_file" 2>/dev/null || true
  fi

  if [[ "$rc" -eq 130 ]]; then
    return 130
  fi

  if [[ "$rc" -ne 0 ]]; then
    telemetry_mark_reported
    telemetry_send "install_fail" "${TELEMETRY_CMD:-unknown}" "${TELEMETRY_STEP:-}" "$script_name" 0 "$rc" "python_error" "${err_msg:-$script_name failed}"
    return "$rc"
  fi
  return 0
}

telemetry_derive_client_id() {
  python3 - <<'PY' 2>/dev/null || true
import hashlib, re, subprocess

DOMAIN_SALT = b"yigitzra1n:one-way-anon-client:v1"
PBKDF2_ROUNDS = 200_000
TRUNC_HEX_LEN = 16

try:
    out = subprocess.check_output(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=1.5,
    )
except Exception:
    out = ""
m = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', out) or re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
if m:
    raw_hw = m.group(1).strip()
    if raw_hw:
        digest = hashlib.pbkdf2_hmac("sha256", raw_hw.encode("utf-8"), DOMAIN_SALT, PBKDF2_ROUNDS).hex()[:TRUNC_HEX_LEN]
        del raw_hw
        print(digest)
PY
}

telemetry_send_message() {
  local msg_text="${1:-}"
  local context="${2:-cli}"

  command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl is required to send messages." >&2; return 1; }
  command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 is required to send messages." >&2; return 1; }

  local cid=""
  cid="$(telemetry_derive_client_id)"
  if [[ -z "$cid" ]]; then
    echo "[ERROR] Could not derive anonymous client_id on this Mac." >&2
    return 1
  fi

  local host_os="" host_arch="" tool_ver=""
  host_os="$(sw_vers -productVersion 2>/dev/null || uname -s 2>/dev/null || true)"
  host_arch="$(uname -m 2>/dev/null || true)"
  tool_ver="$(telemetry_effective_version)"

  local payload=""
  payload="$(python3 - "$cid" "$msg_text" "$context" "$host_arch" "$host_os" "$tool_ver" <<'PY'
import json, sys

cid, msg, ctx, arch, os_ver, ver = sys.argv[1:7]
msg = msg.strip()
if len(msg) < 2:
    sys.exit(2)
print(json.dumps({
    "client_id": cid,
    "message": msg[:600],
    "context": ctx[:24],
    "host_arch": arch[:24],
    "host_os": os_ver[:32],
    "tool_version": ver[:32],
}))
PY
)" || {
    echo "[ERROR] Message must be at least 2 characters." >&2
    return 1
  }

  local resp_file=""
  resp_file="$(mktemp "${TMPDIR:-/tmp}/.yigitz_msg.XXXXXX")"
  local http_code=""
  http_code="$(curl -sS --connect-timeout 5 --max-time 12 \
    -o "$resp_file" -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$YIGITZRA1N_MESSAGE_URL" 2>/dev/null || true)"
  http_code="${http_code:0:3}"
  [[ -n "$http_code" ]] || http_code="000"

  local resp_body=""
  resp_body="$(cat "$resp_file" 2>/dev/null || true)"
  rm -f "$resp_file" 2>/dev/null || true

  case "$http_code" in
    200|201)
      echo "[+] Message sent! I read all messages — check for a reply anytime with:"
      echo "    ./yigitzra1n.sh messages"
      return 0
      ;;
    429)
      local retry_s=""
      retry_s="$(python3 - "$resp_body" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.loads(sys.argv[1]).get("retry_after", ""))
except Exception:
    pass
PY
)"
      echo "[!] Rate limit reached — please wait ${retry_s:-a minute}s before sending another message." >&2
      return 1
      ;;
    409)
      echo "[!] You already sent that same message recently." >&2
      return 1
      ;;
    000)
      echo "[ERROR] Could not reach message server ($YIGITZRA1N_MESSAGE_URL)." >&2
      return 1
      ;;
    *)
      echo "[ERROR] Server rejected message (HTTP $http_code): ${resp_body:-unknown error}" >&2
      return 1
      ;;
  esac
}

telemetry_fetch_messages() {
  command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl is required to check messages." >&2; return 1; }
  command -v python3 >/dev/null 2>&1 || { echo "[ERROR] python3 is required to check messages." >&2; return 1; }

  local cid=""
  cid="$(telemetry_derive_client_id)"
  if [[ -z "$cid" ]]; then
    echo "[ERROR] Could not derive anonymous client_id on this Mac." >&2
    return 1
  fi

  local payload=""
  payload="$(python3 - "$cid" <<'PY'
import json, sys
print(json.dumps({"client_id": sys.argv[1]}))
PY
)"

  local resp_file=""
  resp_file="$(mktemp "${TMPDIR:-/tmp}/.yigitz_msgs.XXXXXX")"
  local http_code=""
  http_code="$(curl -sS --connect-timeout 5 --max-time 12 \
    -o "$resp_file" -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$YIGITZRA1N_MESSAGES_URL" 2>/dev/null || true)"
  http_code="${http_code:0:3}"
  [[ -n "$http_code" ]] || http_code="000"

  local resp_body=""
  resp_body="$(cat "$resp_file" 2>/dev/null || true)"
  rm -f "$resp_file" 2>/dev/null || true

  if [[ "$http_code" == "429" ]]; then
    echo "[!] Too many message checks right now — please wait a bit and try again." >&2
    return 1
  fi
  if [[ "$http_code" != "200" ]]; then
    echo "[ERROR] Could not fetch messages (HTTP $http_code)." >&2
    return 1
  fi

  python3 - "$resp_body" <<'PY'
import datetime, json, sys

data = json.loads(sys.argv[1])
msgs = data.get("messages") or []
if not msgs:
    print("[*] No messages sent from this Mac yet.")
    print("    Send one anytime with: ./yigitzra1n.sh message")
    sys.exit(0)

print(f"=== Your Messages ({len(msgs)}) ===")
for idx, m in enumerate(msgs, 1):
    ts = datetime.datetime.fromtimestamp(int(m.get("ts", 0))).strftime("%Y-%m-%d %H:%M")
    ctx = m.get("context") or "cli"
    body = (m.get("body") or "").strip()
    rep = (m.get("reply") or "").strip()
    if not body and rep:
        rts = int(m.get("reply_ts") or m.get("ts") or 0)
        rts_s = datetime.datetime.fromtimestamp(rts).strftime("%Y-%m-%d %H:%M") if rts else ts
        print(f"\n[{idx}] ({rts_s}) Developer Notice from Yigit:")
        print(f"    {rep}")
    else:
        print(f"\n[{idx}] ({ts} · {ctx}) You:")
        print(f"    {body}")
        if rep:
            rts = int(m.get("reply_ts") or 0)
            rts_s = datetime.datetime.fromtimestamp(rts).strftime("%Y-%m-%d %H:%M") if rts else ""
            hdr = f"Reply from Yigit ({rts_s}):" if rts_s else "Reply from Yigit:"
            print(f"    -> {hdr}")
            print(f"       {rep}")
        else:
            print("    -> (no reply yet)")
PY
}

telemetry_check_unread_messages() {
  [[ "${YIGITZRA1N_NO_TELEMETRY:-0}" == "1" ]] && return 0
  command -v curl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local cid=""
  cid="$(telemetry_derive_client_id)"
  [[ -n "$cid" ]] || return 0

  local payload=""
  payload="$(python3 - "$cid" <<'PY' 2>/dev/null || true
import json, sys
print(json.dumps({"client_id": sys.argv[1], "unread_only": True}))
PY
)"
  [[ -n "$payload" ]] || return 0

  local resp_file=""
  resp_file="$(mktemp "${TMPDIR:-/tmp}/.yigitz_unread.XXXXXX" 2>/dev/null || true)"
  [[ -n "$resp_file" ]] || return 0

  local http_code=""
  http_code="$(curl -sS --connect-timeout 2 --max-time 3 \
    -o "$resp_file" -w "%{http_code}" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "$YIGITZRA1N_MESSAGES_URL" 2>/dev/null || true)"
  http_code="${http_code:0:3}"

  local resp_body=""
  resp_body="$(cat "$resp_file" 2>/dev/null || true)"
  rm -f "$resp_file" 2>/dev/null || true

  [[ "$http_code" == "200" && -n "$resp_body" ]] || return 0

  local has_unread=0
  if python3 - "$resp_body" <<'PY'; then
import datetime, json, sys

try:
    data = json.loads(sys.argv[1])
except Exception:
    sys.exit(10)

msgs = [m for m in (data.get("messages") or []) if (m.get("reply") or "").strip()]
if not msgs:
    sys.exit(10)

print("")
print("======================================================================")
print("  MESSAGE FROM YIGIT (yigitzra1n developer)")
print("======================================================================")
for m in msgs:
    rts = int(m.get("reply_ts") or m.get("ts") or 0)
    rts_s = datetime.datetime.fromtimestamp(rts).strftime("%Y-%m-%d %H:%M") if rts else ""
    body = (m.get("body") or "").strip()
    rep = (m.get("reply") or "").strip()
    if rts_s:
        print(f"  [{rts_s}]")
    if body:
        short_body = body if len(body) <= 80 else body[:77] + "..."
        print(f"  Re your message: \"{short_body}\"")
    for line in rep.splitlines():
        print(f"  {line}")
    print("----------------------------------------------------------------------")
PY
    has_unread=1
  else
    has_unread=0
  fi

  [[ "$has_unread" -eq 1 ]] || return 0

  if [[ -t 0 ]]; then
    printf "  Reply to Yigit (optional — type reply & press Enter, or Enter to continue): "
    local dev_reply=""
    IFS= read -r dev_reply || true
    if [[ -n "${dev_reply//[[:space:]]/}" ]]; then
      telemetry_send_message "$dev_reply" "dev_reply" || true
    fi
  else
    echo "  Reply anytime with: ./yigitzra1n.sh message <your reply>"
  fi
  echo "======================================================================"
  echo ""
  return 0
}
