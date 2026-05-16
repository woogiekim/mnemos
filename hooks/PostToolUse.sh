#!/usr/bin/env bash
# hooks/PostToolUse.sh
#
# Claude Code PostToolUse hook for mnemos autonomous background operations.
#
# Fires after every Claude tool call (Write, Edit, Bash, Read, etc.).
# Runs a throttled background check that:
#   - Archives stale/low-quality memories (GC)
#   - Auto-promotes session memories to project/global when eligible
#   - Detects and flags duplicate memories
#
# Throttling: the check runs at most once per MNEMOS_BG_INTERVAL_MINUTES
# (default: 5 minutes), enforced via a /tmp timestamp file.
# Silent when no notable activity occurs; emits a brief
# <mnemos-context type="background-activity"> block when it does.
#
# Environment variables:
#   MNEMOS_REPO_ROOT          — path to the mnemos repository (required)
#   MNEMOS_BG_INTERVAL_MINUTES — throttle interval in minutes (default: 5)

set -euo pipefail

# ---------------------------------------------------------------------------
# Guard: mnemos must be available and REPO_ROOT must be set
# ---------------------------------------------------------------------------
if [ -z "${MNEMOS_REPO_ROOT:-}" ]; then
  exit 0
fi

if ! command -v mnemos >/dev/null 2>&1; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Fast path: check timestamp file BEFORE spawning Python
# ---------------------------------------------------------------------------
# This pre-check avoids the Python startup cost entirely for the common
# "not enough time has passed" case. The interval is read from the env or
# defaults to 5 minutes.
INTERVAL_MINUTES="${MNEMOS_BG_INTERVAL_MINUTES:-5}"
UID_VAL="${UID:-0}"
TS_FILE="/tmp/mnemos-bg-check-${UID_VAL}.ts"

if [ -f "${TS_FILE}" ]; then
  NOW="$(date +%s)"
  MTIME=""
  # macOS: stat -f%m; Linux: stat -c%Y
  if stat -f%m "${TS_FILE}" >/dev/null 2>&1; then
    MTIME="$(stat -f%m "${TS_FILE}")"
  elif stat -c%Y "${TS_FILE}" >/dev/null 2>&1; then
    MTIME="$(stat -c%Y "${TS_FILE}")"
  fi
  if [ -n "${MTIME}" ]; then
    ELAPSED=$(( NOW - MTIME ))
    THRESHOLD=$(( INTERVAL_MINUTES * 60 ))
    if [ "${ELAPSED}" -lt "${THRESHOLD}" ]; then
      # Not enough time has passed — skip without spawning Python
      exit 0
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Run the background check
# ---------------------------------------------------------------------------
# The `mnemos bg-check` command handles all the logic (GC, auto-promote,
# dedup) and emits a <mnemos-context> block only when it has something to
# report.  Redirect stderr to /dev/null so hook errors are silent.

RESULT="$(
  env MNEMOS_REPO_ROOT="${MNEMOS_REPO_ROOT}" \
  mnemos bg-check \
    --interval "${INTERVAL_MINUTES}" \
  2>/dev/null || true
)"

# Emit to stdout only when there is actual content (non-empty, non-whitespace)
if [ -n "${RESULT// /}" ]; then
  printf '%s\n' "${RESULT}"
fi

exit 0
