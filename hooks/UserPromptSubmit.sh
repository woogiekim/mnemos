#!/usr/bin/env bash
# hooks/UserPromptSubmit.sh
#
# Claude Code UserPromptSubmit hook for mnemos active memory retrieval.
#
# Receives the user prompt as JSON on stdin:
#   { "session_id": "...", "transcript_path": "...", "hook_event_name": "...", "prompt": "..." }
#
# Outputs context injected before Claude processes the prompt.
#
# Behaviour:
#   1. Session-start load: on the first prompt of a session, load all project
#      and global layer memories and inject them as standing context.
#   2. /compact shortcut: emit a reminder to capture session insights before compacting.
#   3. Per-prompt active search: extract key terms from the prompt and run
#      `mnemos search` to surface relevant memories (top 5 results).
#
# Environment variables:
#   MNEMOS_REPO_ROOT   — path to the mnemos repository (required)
#   MNEMOS_HOOK_TIMEOUT — seconds before search is aborted (default: 8)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
TIMEOUT="${MNEMOS_HOOK_TIMEOUT:-8}"
SEARCH_LIMIT=5
SESSION_START_LIMIT=30

# ---------------------------------------------------------------------------
# Guard: mnemos must be available and REPO_ROOT must be set
# ---------------------------------------------------------------------------
if [ -z "${REPO_ROOT}" ]; then
  exit 0
fi

if ! command -v mnemos >/dev/null 2>&1; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Read JSON input from stdin
# ---------------------------------------------------------------------------
INPUT="$(cat)"

# Extract fields using python3 (always available alongside mnemos)
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
PROMPT="$(read_field prompt)"

if [ -z "${PROMPT}" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# /compact special case
# ---------------------------------------------------------------------------
if [ "${PROMPT}" = "/compact" ]; then
  echo "[mnemos] /compact detected — before compacting, capture the key insights, decisions, and context from this session using mnemos capture --quiet. Run mnemos capture --quiet for each significant item now."
  exit 0
fi

# ---------------------------------------------------------------------------
# Session-start context load (project + global layers)
# ---------------------------------------------------------------------------
# Use a per-session flag file to inject standing context only once per session.
SESSION_FLAG_DIR="${TMPDIR:-/tmp}/mnemos-session-flags"
mkdir -p "${SESSION_FLAG_DIR}" 2>/dev/null || true

SESSION_KEY="$(echo "${SESSION_ID}" | tr -cd 'a-zA-Z0-9_-' | cut -c1-64)"
SESSION_FLAG="${SESSION_FLAG_DIR}/mnemos-session-loaded-${SESSION_KEY}"

if [ -n "${SESSION_KEY}" ] && [ ! -f "${SESSION_FLAG}" ]; then
  # Mark session as loaded immediately to prevent duplicate loads
  touch "${SESSION_FLAG}" 2>/dev/null || true

  STANDING_CONTEXT="$(
    timeout "${TIMEOUT}" \
      env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
      mnemos list --layer project,global --limit "${SESSION_START_LIMIT}" \
      2>/dev/null || true
  )"

  if [ -n "${STANDING_CONTEXT}" ]; then
    echo "<mnemos-context type=\"session-start\" layers=\"project,global\">"
    echo "${STANDING_CONTEXT}"
    echo "</mnemos-context>"
    echo ""
  fi
fi

# ---------------------------------------------------------------------------
# Per-prompt active search
# ---------------------------------------------------------------------------
# Extract a concise query from the prompt (first 200 chars, stripped of
# punctuation and stop words by the mnemos FTS engine).
QUERY="${PROMPT:0:200}"

SEARCH_RESULTS="$(
  timeout "${TIMEOUT}" \
    env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
    mnemos search "${QUERY}" --limit "${SEARCH_LIMIT}" \
    2>/dev/null || true
)"

# Only emit if real results were found (not the "no results found" sentinel)
if [ -n "${SEARCH_RESULTS}" ] && echo "${SEARCH_RESULTS}" | grep -qv "^no results found$"; then
  # Filter out the summary line "([mnemos] Retrieved N memories)" when there
  # are actual result lines above it.
  RESULT_LINES="$(echo "${SEARCH_RESULTS}" | grep -v '^\[mnemos\]' || true)"
  if [ -n "${RESULT_LINES}" ]; then
    echo "<mnemos-context type=\"search\" query=\"${QUERY:0:60}\">"
    echo "${RESULT_LINES}"
    echo "</mnemos-context>"
  fi
fi

exit 0
