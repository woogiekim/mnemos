#!/usr/bin/env bash
# Claude Code PostToolUse hook for throttled mnemos maintenance.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
if [[ -z "${REPO_ROOT}" ]]; then
  exit 0
fi
if ! source "${SCRIPT_DIR}/hook_input.sh"; then
  exit 0
fi
if ! mnemos_read_json_payload; then
  exit 0
fi
PAYLOAD="${MNEMOS_HOOK_PAYLOAD}"

RESULT_FILE="${MNEMOS_BG_RESULT_FILE:-/tmp/mnemos-bg-check-${UID:-0}.result}"
if [[ -f "${RESULT_FILE}" ]]; then
  DRAIN_FILE="${RESULT_FILE}.drain.$$"
  if mv "${RESULT_FILE}" "${DRAIN_FILE}" 2>/dev/null; then
    cat "${DRAIN_FILE}" 2>/dev/null
    rm -f "${DRAIN_FILE}" 2>/dev/null
  fi
fi

if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi
if command -v nohup >/dev/null 2>&1; then
  printf '%s' "${PAYLOAD}" \
    | MNEMOS_POST_TOOL_DRAIN_RESULT=0 nohup python3 -S "${SCRIPT_DIR}/post_tool_use.py" >/dev/null 2>&1 &
else
  printf '%s' "${PAYLOAD}" \
    | MNEMOS_POST_TOOL_DRAIN_RESULT=0 python3 -S "${SCRIPT_DIR}/post_tool_use.py" >/dev/null 2>&1 &
fi
exit 0
