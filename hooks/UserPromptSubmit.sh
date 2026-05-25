#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook for mnemos deterministic V1 context injection.

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

[ -n "${REPO_ROOT}" ] || exit 0
command -v mnemos >/dev/null 2>&1 || exit 0

INPUT="$(cat)"

read_field() {
  local field="$1"
  python3 -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get('${field}', ''))
except Exception:
    print('')
" "${INPUT}" 2>/dev/null || true
}

SESSION_ID="$(read_field session_id)"
export MNEMOS_SESSION_ID="${SESSION_ID}"
PROMPT="$(read_field prompt)"

[ -n "${PROMPT}" ] || exit 0

SESSION_FLAG_DIR="${TMPDIR:-/tmp}/mnemos-session-flags"
mkdir -p "${SESSION_FLAG_DIR}" 2>/dev/null || true
SESSION_KEY="$(echo "${SESSION_ID}" | tr -cd 'a-zA-Z0-9_-' | cut -c1-64)"
if [ -n "${SESSION_KEY}" ]; then
  touch "${SESSION_FLAG_DIR}/mnemos-session-loaded-${SESSION_KEY}" 2>/dev/null || true
fi

if [ "${PROMPT}" = "/compact" ]; then
  echo "[mnemos] /compact detected — manual fallback: capture key insights with mnemos capture before compacting."
  exit 0
fi

CONTEXT_BLOCK="$(
  _timeout "${TIMEOUT}" \
    env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
    PYTHONPATH="${PYTHONPATH_VALUE}" \
    mnemos context --render \
      --prompt "${PROMPT}" \
      --session-id "${SESSION_ID}" \
      --host "claude-code" \
    2>/dev/null || true
)"

[ -n "${CONTEXT_BLOCK}" ] && echo "${CONTEXT_BLOCK}"
exit 0
