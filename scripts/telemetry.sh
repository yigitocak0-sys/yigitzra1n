#!/usr/bin/env bash

YIGITZRA1N_TELEMETRY_URL="${YIGITZRA1N_TELEMETRY_URL:-https://telemetry.yigitz.com/v1/event}"
YIGITZRA1N_NO_TELEMETRY="${YIGITZRA1N_NO_TELEMETRY:-0}"
YIGITZRA1N_TOOL_VERSION="${YIGITZRA1N_TOOL_VERSION:-1.0}"

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

telemetry_send() {
  local event="${1:-install_fail}"
  local command_name="${2:-unknown}"
  local step="${3:-}"
  local source_file="${4:-}"
  local line="${5:-0}"
  local exit_code="${6:-0}"
  local reason="${7:-}"
  local error_msg="${8:-}"

  if [[ "$event" == "install_fail" || "$event" == "security_block" ]]; then
    telemetry_print_contact_note
  fi

  [[ "${YIGITZRA1N_NO_TELEMETRY:-0}" == "1" ]] && return 0
  command -v curl >/dev/null 2>&1 || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  local host_os=""
  local host_arch=""
  host_os="$(sw_vers -productVersion 2>/dev/null || uname -s 2>/dev/null || true)"
  host_arch="$(uname -m 2>/dev/null || true)"

  local payload=""
  payload="$(python3 - "$event" "$command_name" "$step" "$source_file" "$line" "$exit_code" "$reason" "$error_msg" "$host_arch" "$host_os" "$YIGITZRA1N_TOOL_VERSION" <<'PY' 2>/dev/null || true
import json, re, sys

def clean(s, n):
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", s).strip()
    s = re.sub(r"/(?:Users|home)/[^/\s:\"']+", "/<user>", s)
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
    "event": clean(sys.argv[1], 32),
    "command": clean(sys.argv[2], 24),
    "step": clean(sys.argv[3], 48),
    "source_file": clean(sys.argv[4], 64),
    "line": line_num,
    "exit_code": rc,
    "reason": clean(sys.argv[7], 64),
    "error_msg": clean(sys.argv[8], 240),
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

  telemetry_was_reported && return 0
  telemetry_mark_reported
  telemetry_send "install_fail" "$command_name" "$step" "$source_file" "$line" "$exit_code" "command_failed" "$failed_cmd"
}
