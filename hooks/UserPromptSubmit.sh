#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook for mnemos deterministic V1 context injection.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
if [[ -z "${REPO_ROOT}" ]]; then
  exit 0
fi
if ! source "${SCRIPT_DIR}/hook_input.sh"; then
  exit 0
fi
set -o pipefail

if ! mnemos_read_json_payload route; then
  exit 0
fi
PAYLOAD="${MNEMOS_HOOK_PAYLOAD}"

CACHE_DIR="${MNEMOS_CONTEXT_CACHE_DIR:-${TMPDIR:-/tmp}/mnemos-context-cache}"
SYNC_OUTPUT=0
if [[ -n "${MNEMOS_HOOK_ROUTE_STATUS}" ]]; then
  if [[ "${MNEMOS_HOOK_ROUTE_STATUS}" == "0" \
    || "${MNEMOS_HOOK_ROUTE_STATUS}" -gt "1" ]]; then
    SYNC_OUTPUT=1
  fi
else
  if [[ "${PAYLOAD}" == *'"/compact"'* ]]; then
    SYNC_OUTPUT=1
  elif [[ -n "${MNEMOS_CONTEXT_CACHE_FILE:-}" ]] \
    && mnemos_cache_file_fresh "${MNEMOS_CONTEXT_CACHE_FILE}"; then
    SYNC_OUTPUT=1
  else
    mnemos_default_cache_hit "${PAYLOAD}" "${REPO_ROOT}" "${CACHE_DIR}"
    CACHE_STATUS=$?
    if [[ "${CACHE_STATUS}" == "0" || "${CACHE_STATUS}" == "2" ]]; then
      SYNC_OUTPUT=1
    fi
  fi
  if mnemos_promotion_output_possible "${REPO_ROOT}"; then
    SYNC_OUTPUT=1
  fi
fi

if [[ "${SYNC_OUTPUT}" == "1" ]]; then
  printf '%s' "${PAYLOAD}" | python3 -S "${SCRIPT_DIR}/user_prompt_submit.py"
  exit $?
fi

if command -v nohup >/dev/null 2>&1; then
  printf '%s' "${PAYLOAD}" \
    | MNEMOS_RENDER_PROMOTIONS=0 nohup python3 -S "${SCRIPT_DIR}/user_prompt_submit.py" >/dev/null 2>&1 &
else
  printf '%s' "${PAYLOAD}" \
    | MNEMOS_RENDER_PROMOTIONS=0 python3 -S "${SCRIPT_DIR}/user_prompt_submit.py" >/dev/null 2>&1 &
fi
exit 0
