#!/usr/bin/env bash
# Claude Code PostToolUse hook for throttled mnemos maintenance.
# Heavy work runs in a detached worker and never blocks the tool response.

set -u

INTERVAL_MINUTES="${MNEMOS_BG_INTERVAL_MINUTES:-5}"
UID_VAL="${UID:-0}"
TS_FILE="${MNEMOS_BG_TS_FILE:-/tmp/mnemos-bg-check-${UID_VAL}.ts}"
LOCK_FILE="${MNEMOS_BG_LOCK_DIR:-/tmp/mnemos-bg-check-${UID_VAL}.lock}"
RESULT_FILE="${MNEMOS_BG_RESULT_FILE:-/tmp/mnemos-bg-check-${UID_VAL}.result}"

mtime_of() {
  local path="$1"
  if stat -f%m "${path}" >/dev/null 2>&1; then
    stat -f%m "${path}"
    return
  fi
  stat -c%Y "${path}" 2>/dev/null || printf '0'
}

is_recent() {
  local path="$1" threshold="$2"
  [ -e "${path}" ] || return 1
  local now mtime
  now="$(date +%s)"
  mtime="$(mtime_of "${path}")"
  [ $((now - mtime)) -lt "${threshold}" ]
}

[ -n "${MNEMOS_REPO_ROOT:-}" ] || exit 0
command -v mnemos >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Deliver completed maintenance output on a later tool event. Atomic rename
# ensures that concurrent sessions cannot emit the same result twice.
if [ -f "${RESULT_FILE}" ]; then
  drain_file="${RESULT_FILE}.drain.$$"
  if mv "${RESULT_FILE}" "${drain_file}" 2>/dev/null; then
    cat "${drain_file}"
    rm -f "${drain_file}"
  fi
fi

threshold=$((INTERVAL_MINUTES * 60))
is_recent "${TS_FILE}" "${threshold}" && exit 0

# The worker performs the authoritative timestamp recheck while holding an OS
# file lock. flock is released by the kernel even if the worker is killed, so a
# stale lock artifact cannot block future maintenance.
nohup env \
  MNEMOS_REPO_ROOT="${MNEMOS_REPO_ROOT}" \
  MNEMOS_BG_INTERVAL_MINUTES="${INTERVAL_MINUTES}" \
  MNEMOS_BG_TS_FILE="${TS_FILE}" \
  MNEMOS_BG_LOCK_FILE="${LOCK_FILE}" \
  MNEMOS_BG_RESULT_FILE="${RESULT_FILE}" \
  python3 "$(dirname "$0")/post_tool_worker.py" \
  </dev/null >/dev/null 2>&1 &

exit 0
