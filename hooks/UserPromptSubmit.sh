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

# macOS does not ship `timeout`; use gtimeout (coreutils) or perl fallback.
_timeout() {
  local secs="$1"; shift
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  elif command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  else
    # No timeout binary — run without a hard limit (hook itself has a short budget).
    "$@"
  fi
}

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
    _timeout "${TIMEOUT}" \
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
# Per-prompt active search — keyword extraction + per-keyword search + dedup
# ---------------------------------------------------------------------------
# Extract keywords from the prompt using an inline python3 snippet, then
# search each keyword individually and merge the results (dedup by ID).

KEYWORDS="$(python3 -c "
import re, sys

ENGLISH_STOPWORDS = {
    'the','a','an','is','are','was','were','be','been','have','has','had',
    'do','does','did','will','would','could','should','may','might','shall',
    'must','can','to','of','in','on','at','by','for','from','with','and',
    'or','but','not','so','if','as','it','its','this','that','these','those',
    'i','you','we','they','my','your','our','their','what','how','why',
    'when','where','which',
}
KOREAN_STOPWORDS = {
    '이','가','은','는','을','를','의','에','에서','으로','로','와','과',
    '하고','도','만','이다','있다','없다','했다','합니다','해요','거','것',
    '수','좀','그','저','제','네','아',
}

prompt = sys.argv[1][:500]

# Split camelCase/PascalCase before further tokenisation
def split_camel(token):
    parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', token)
    parts = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', parts)
    return parts.split()

# Tokenise on whitespace and punctuation
raw_tokens = re.split(r'[\s\W]+', prompt)

words = []
for tok in raw_tokens:
    words.extend(split_camel(tok))

# Normalise, filter stopwords and short tokens
seen = set()
keywords = []
for w in words:
    lw = w.lower()
    if len(w) < 2:
        continue
    if lw in ENGLISH_STOPWORDS or w in KOREAN_STOPWORDS:
        continue
    if lw in seen:
        continue
    seen.add(lw)
    keywords.append(w)

# Pick up to 5 longest/most unique keywords
keywords.sort(key=lambda x: -len(x))
print('\n'.join(keywords[:5]))
" "${PROMPT}" 2>/dev/null || true)"

# Collect unique result lines across all keyword searches (dedup by first token).
# Use a temp file for seen-keys tracking to stay compatible with bash 3.x.
SEEN_KEYS_FILE="$(mktemp)" || SEEN_KEYS_FILE=""
MERGED_LINES=""
FIRST_KEYWORD=""

while IFS= read -r KW; do
  [ -z "${KW}" ] && continue
  [ -z "${FIRST_KEYWORD}" ] && FIRST_KEYWORD="${KW}"

  KW_RESULTS="$(
    _timeout "${TIMEOUT}" \
      env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
      mnemos search "${KW}" --limit 3 \
      2>/dev/null || true
  )"

  [ -z "${KW_RESULTS}" ] && continue
  echo "${KW_RESULTS}" | grep -q "^no results found" && continue

  # Filter summary/empty lines, then dedup by first whitespace-delimited token.
  while IFS= read -r LINE; do
    [ -z "${LINE}" ] && continue
    echo "${LINE}" | grep -q '^\[mnemos\]' && continue

    KEY="${LINE%% *}"
    # Skip if this key has already been seen.
    if [ -n "${SEEN_KEYS_FILE}" ] && grep -qxF "${KEY}" "${SEEN_KEYS_FILE}" 2>/dev/null; then
      continue
    fi
    [ -n "${SEEN_KEYS_FILE}" ] && printf '%s\n' "${KEY}" >> "${SEEN_KEYS_FILE}"

    if [ -z "${MERGED_LINES}" ]; then
      MERGED_LINES="${LINE}"
    else
      MERGED_LINES="${MERGED_LINES}"$'\n'"${LINE}"
    fi
    # Stop once we have reached the result limit.
    if [ "$(printf '%s\n' "${MERGED_LINES}" | wc -l)" -ge "${SEARCH_LIMIT}" ]; then
      break 2
    fi
  done <<< "${KW_RESULTS}"
done <<< "${KEYWORDS}"

# Clean up temp file.
[ -n "${SEEN_KEYS_FILE}" ] && rm -f "${SEEN_KEYS_FILE}" 2>/dev/null || true

# Only emit if real results were found.
if [ -n "${MERGED_LINES}" ]; then
  echo "<mnemos-context type=\"search\" query=\"${FIRST_KEYWORD:0:60}\">"
  echo "${MERGED_LINES}"
  echo "</mnemos-context>"
fi

exit 0
