#!/usr/bin/env bash
# Claude Code Stop hook for mnemos deterministic transcript capture.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
if ! source "${SCRIPT_DIR}/hook_input.sh"; then
  exit 0
fi
if ! mnemos_read_json_payload; then
  exit 0
fi

if command -v nohup >/dev/null 2>&1; then
  printf '%s' "${MNEMOS_HOOK_PAYLOAD}" \
    | nohup python3 -S "${SCRIPT_DIR}/stop_hook.py" >/dev/null 2>&1 &
else
  printf '%s' "${MNEMOS_HOOK_PAYLOAD}" \
    | python3 -S "${SCRIPT_DIR}/stop_hook.py" >/dev/null 2>&1 &
fi
exit 0
