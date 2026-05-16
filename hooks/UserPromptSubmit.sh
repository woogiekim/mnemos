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
#   4. Per-prompt capture reminder: inject a <mnemos-capture-protocol> block that
#      instructs Claude to call `mnemos capture --quiet` after each response turn,
#      so the memory base grows organically from conversations.
#   5. Observability: log every hook invocation, search call, and session-start
#      event to wiki/observability.jsonl (async background write, zero latency impact).
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
# Observability helper — async background write to observability.jsonl
# Use python3 inline so the write never blocks the hook response.
# ---------------------------------------------------------------------------
_obs_append() {
  # $1 = JSON object string (already valid JSON, caller's responsibility)
  local json_line="$1"
  local obs_path="${REPO_ROOT}/wiki/observability.jsonl"
  python3 - "${obs_path}" "${json_line}" <<'PYEOF' &
import json, os, sys, datetime
obs_path, json_line = sys.argv[1], sys.argv[2]
try:
    os.makedirs(os.path.dirname(obs_path), exist_ok=True)
    with open(obs_path, "a", encoding="utf-8") as f:
        f.write(json_line + "\n")
except Exception:
    pass
PYEOF
}

_obs_event() {
  # Build and append an observability JSON line.
  # Args: event session_id [extra_json_pairs...]
  # extra_json_pairs: key=value pairs appended as JSON fields (string values only).
  local event="$1"
  local session="$2"
  shift 2
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  # Build extra fields from remaining key=value args via python3
  local extras_json="{}"
  if [ $# -gt 0 ]; then
    extras_json="$(python3 -c "
import json, sys
pairs = sys.argv[1:]
d = {}
for p in pairs:
    if '=' in p:
        k, _, v = p.partition('=')
        try:
            d[k] = json.loads(v)
        except Exception:
            d[k] = v
print(json.dumps(d))
" "$@" 2>/dev/null || echo '{}')"
  fi

  local entry
  entry="$(python3 -c "
import json, sys
base = {'ts': sys.argv[1], 'event': sys.argv[2], 'agent': 'claude', 'session_id': sys.argv[3]}
extra = json.loads(sys.argv[4])
base.update(extra)
print(json.dumps(base, ensure_ascii=False))
" "${ts}" "${event}" "${session}" "${extras_json}" 2>/dev/null || true)"

  [ -n "${entry}" ] && _obs_append "${entry}"
}

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

SESSION_MEMORY_COUNT=0

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
    # Count loaded memories (lines that start with whitespace then [layer])
    SESSION_MEMORY_COUNT="$(echo "${STANDING_CONTEXT}" | grep -c '^\s*\[' 2>/dev/null || echo 0)"

    echo "<mnemos-context type=\"session-start\" layers=\"project,global\">"
    echo "${STANDING_CONTEXT}"
    echo "</mnemos-context>"
    echo ""
  fi

  # Brief stats summary at session-start (3-4 lines max)
  STATS_SUMMARY="$(
    python3 -c "
import json, os, datetime, collections
obs_path = os.path.join(os.environ.get('MNEMOS_REPO_ROOT', ''), 'wiki', 'observability.jsonl')
try:
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ')
    captures = collections.defaultdict(int)
    hook_calls = 0
    last_gc_ts = None
    last_gc_count = 0
    with open(obs_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get('ts', '')
            ev = e.get('event', '')
            if ts >= cutoff:
                if ev == 'capture':
                    captures[e.get('layer', 'unknown')] += 1
                if ev in ('hook_search', 'hook_session_start'):
                    hook_calls += 1
            if ev == 'gc' and (last_gc_ts is None or ts > last_gc_ts):
                last_gc_ts = ts
                last_gc_count = e.get('archived_count', 0)
    total_cap = sum(captures.values())
    proj = captures.get('project', 0)
    gl = captures.get('global', 0)
    parts = [f'captures this week: {total_cap} (project={proj}, global={gl})',
             f'hook calls this week: {hook_calls}']
    if last_gc_ts:
        parts.append(f'last GC: {last_gc_ts[:10]} (archived {last_gc_count})')
    print(' | '.join(parts))
except FileNotFoundError:
    pass
except Exception:
    pass
" 2>/dev/null || true
  )"

  if [ -n "${STATS_SUMMARY}" ]; then
    echo "<mnemos-context type=\"stats\">"
    echo "${STATS_SUMMARY}"
    echo "</mnemos-context>"
    echo ""
  fi

  # Observability: log session-start event (async)
  _obs_event "hook_session_start" "${SESSION_ID}" \
    "memory_count=${SESSION_MEMORY_COUNT}"
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
ALL_KEYWORDS_LIST=""
SEARCH_RESULT_IDS=""

while IFS= read -r KW; do
  [ -z "${KW}" ] && continue
  [ -z "${FIRST_KEYWORD}" ] && FIRST_KEYWORD="${KW}"

  # Track all keywords used (for observability log)
  if [ -z "${ALL_KEYWORDS_LIST}" ]; then
    ALL_KEYWORDS_LIST="${KW}"
  else
    ALL_KEYWORDS_LIST="${ALL_KEYWORDS_LIST},${KW}"
  fi

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

    # Extract memory ID for observability from [fts] header lines only.
    # Format: "  [fts] <memory-id>: <preview>"
    # Content lines (title:, applies_to:, etc.) must NOT be used as IDs.
    if echo "${LINE}" | grep -qE '^\s+\[fts\] '; then
      RESULT_ID="$(echo "${LINE}" | sed 's/^[[:space:]]*\[fts\] \([0-9a-f-]*\):.*/\1/')"
      [ -n "${RESULT_ID}" ] && SEARCH_RESULT_IDS="${SEARCH_RESULT_IDS:+${SEARCH_RESULT_IDS},}${RESULT_ID}"
    fi

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

# ---------------------------------------------------------------------------
# Observability: log the search event (async, fires even if no results)
# ---------------------------------------------------------------------------
if [ -n "${ALL_KEYWORDS_LIST}" ]; then
  RESULT_COUNT="$(echo "${MERGED_LINES}" | grep -c . 2>/dev/null || echo 0)"
  [ -z "${MERGED_LINES}" ] && RESULT_COUNT=0

  # Build a compact JSON result list from the collected IDs
  RESULTS_JSON="$(python3 -c "
import json, sys
ids_str = sys.argv[1]
ids = [i.strip() for i in ids_str.split(',') if i.strip()] if ids_str else []
print(json.dumps([{'id': i, 'score': 0.0} for i in ids]))
" "${SEARCH_RESULT_IDS:-}" 2>/dev/null || echo '[]')"

  KW_JSON="$(python3 -c "
import json, sys
kws = [k.strip() for k in sys.argv[1].split(',') if k.strip()]
print(json.dumps(kws))
" "${ALL_KEYWORDS_LIST}" 2>/dev/null || echo '[]')"

  OBS_ENTRY="$(python3 -c "
import json, sys, datetime
ts = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
entry = {
    'ts': ts,
    'event': 'hook_search',
    'agent': 'claude',
    'session_id': sys.argv[1],
    'keywords': json.loads(sys.argv[2]),
    'results': json.loads(sys.argv[3]),
    'result_count': int(sys.argv[4]),
}
print(json.dumps(entry, ensure_ascii=False))
" "${SESSION_ID}" "${KW_JSON}" "${RESULTS_JSON}" "${RESULT_COUNT}" 2>/dev/null || true)"

  [ -n "${OBS_ENTRY}" ] && _obs_append "${OBS_ENTRY}"
fi

# ---------------------------------------------------------------------------
# Capture protocol reminder — injected on every prompt turn.
#
# This block tells Claude to proactively call `mnemos capture` after
# each response, so the memory base grows organically from conversations.
# Emitted unconditionally (even when no search results were found) because
# the reminder should fire regardless of whether past memories were surfaced.
# ---------------------------------------------------------------------------
echo ""
echo "<mnemos-capture-protocol>"
echo "After responding, proactively capture new insights using:"
echo "  mnemos capture --content \"<insight>\" --quiet --layer session"
echo "Capture liberally — when in doubt, capture. Aim for at least 1 capture per"
echo "substantive response. Capture anything from:"
echo "  - decisions, user preferences, architecture choices, workflow patterns"
echo "  - facts about the project, tools, or environment the user revealed"
echo "  - surprising findings, root causes discovered, constraints identified"
echo "  - anything a future session would benefit from knowing"
echo "Do NOT capture: trivial yes/no answers, simple file lookups, or steps already"
echo "visible in the code or git history."
echo "Notify the user inline: ✻ 🧠 <brief description> (session)"
echo "</mnemos-capture-protocol>"

exit 0
