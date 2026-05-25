#!/usr/bin/env bash
# Claude Code Stop hook for mnemos deterministic transcript capture.

set -euo pipefail

REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
TIMEOUT="${MNEMOS_HOOK_TIMEOUT:-8}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHONPATH_VALUE="${SOURCE_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

_timeout() {
  local secs="$1"; shift
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  elif command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  else
    "$@"
  fi
}

if [ -z "${REPO_ROOT}" ]; then
  exit 0
fi

if ! command -v mnemos >/dev/null 2>&1; then
  exit 0
fi

INPUT="$(cat)"

read_field() {
  local field="$1"
  python3 -c "
import json, sys
try:
    payload = json.loads(sys.argv[1])
    print(payload.get('${field}', ''))
except Exception:
    print('')
" "${INPUT}" 2>/dev/null || true
}

SESSION_ID="$(read_field session_id)"
TRANSCRIPT_PATH="$(read_field transcript_path)"

if [ -z "${TRANSCRIPT_PATH}" ] || [ ! -f "${TRANSCRIPT_PATH}" ]; then
  exit 0
fi

_timeout "${TIMEOUT}" \
  env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
  PYTHONPATH="${PYTHONPATH_VALUE}" \
  mnemos capture-transcript --json \
    --transcript-path "${TRANSCRIPT_PATH}" \
    --session-id "${SESSION_ID}" \
    --host "claude-code" \
  >/dev/null 2>&1 || true

exit 0
