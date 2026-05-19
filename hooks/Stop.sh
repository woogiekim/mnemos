#!/usr/bin/env bash
# hooks/Stop.sh
#
# Claude Code Stop hook for mnemos — parse ✻ emoji markers from the
# completed conversation transcript and capture any not yet persisted.
#
# This hook was originally removed in commit 0de1c47 (Issue #4) because
# Claude Code's Stop event fires once per AI response turn (not at true
# session end), causing the hook to flood the session layer with a new
# capture on every turn.
#
# Re-introduction contract (safe because of on-write dedup)
# ----------------------------------------------------------
# gateway.capture() now maintains a per-gateway-instance in-memory dedup
# registry keyed by (layer, SHA-256(NFKC-normalised content)).  Duplicate
# captures within the same process are silent no-ops — the storage, FTS,
# audit log, and event-bus paths are never reached.
#
# In practice:
#   - Turn N: Claude emits ✻ 💡 "decision X".  Stop hook fires, parses
#     the marker, calls `mnemos capture --content "decision X"`.  The
#     gateway writes the item and records its hash in _capture_dedup.
#   - Turn N+1: same marker appears again.  Stop hook calls capture again.
#     The gateway sees the hash in _capture_dedup and returns None (silent
#     no-op).  The item is NOT written a second time.
#
# This idempotency guarantee is the anti-regression contract tested in
# tests/test_gateway.py::TestCaptureDedup.
#
# Receives JSON on stdin (same shape as other hooks):
#   { "session_id": "...", "transcript_path": "...", "hook_event_name": "Stop" }
#
# Behaviour:
#   1. Read the conversation transcript.
#   2. Mask fenced code blocks (``` ... ```) and inline code (` ... `) before
#      regex matching so capture markers inside code examples are ignored.
#   3. Parse ✻ emoji markers in BOTH forms (grace period — 2 weeks from
#      the feat/emoji-layer-markers-and-dedup merge):
#        New:    ✻ 💡|💾|🧠 <brief>
#        Legacy: ✻ 💡|💾|🧠 <brief> (session|project|global)
#   4. If no explicit marker exists, fall back to the latest meaningful
#      assistant completion summary / session insight, after filtering
#      pipeline control signals, handoffs, and trivial acknowledgements.
#   5. For each parsed insight, call `mnemos capture` with the brief as
#      content and the mapped layer.  The gateway's on-write dedup ensures
#      re-parsing the same turn produces exactly one persisted item.
#
# Environment variables:
#   MNEMOS_REPO_ROOT   — path to the mnemos repository (required)
#   MNEMOS_HOOK_TIMEOUT — seconds before capture is aborted (default: 8)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
TIMEOUT="${MNEMOS_HOOK_TIMEOUT:-8}"

# macOS does not ship `timeout`; use gtimeout (coreutils) or perl fallback.
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

# ---------------------------------------------------------------------------
# Guards
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
TRANSCRIPT_PATH="$(read_field transcript_path)"

if [ -z "${TRANSCRIPT_PATH}" ] || [ ! -f "${TRANSCRIPT_PATH}" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Parse markers from transcript — mask code blocks first
# ---------------------------------------------------------------------------
# Extract ✻ emoji marker lines from the transcript, ignoring markers that
# appear inside fenced (``` ... ```) or inline (` ... `) code spans.
# The masking is done in Python to handle multi-line fenced blocks safely.

MARKERS="$(python3 - "${TRANSCRIPT_PATH}" <<'PYEOF' 2>/dev/null || true
import json, re, sys

transcript_path = sys.argv[1]

try:
    with open(transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Collect all assistant message text
    texts = []
    for msg in data if isinstance(data, list) else []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
except Exception:
    sys.exit(0)

def mask_code(text):
    # Step 1: mask fenced code blocks (``` ... ```) — multi-line aware
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Step 2: mask inline code (` ... `) — single-line
    return re.sub(r"`[^`\n]*`", "", text)

full_text = mask_code("\n".join(texts))

# Step 3: extract ✻ emoji markers
# Emoji → layer mapping
EMOJI_TO_LAYER = {"💡": "session", "💾": "project", "🧠": "global"}

# Pattern matches both new (emoji-only) and legacy (layer) suffix forms:
#   ✻ 💡 <brief>                   ← new
#   ✻ 🧠 <brief> (session)          ← legacy (any layer name in parens)
MARKER_RE = re.compile(
    r"✻\s+([💡💾🧠])\s+(.+?)(?:\s+\([a-z]+\))?\s*$",
    re.MULTILINE,
)

seen = set()
marker_count = 0
for m in MARKER_RE.finditer(full_text):
    emoji = m.group(1)
    brief = m.group(2).strip()
    layer = EMOJI_TO_LAYER.get(emoji, "session")
    key = (layer, brief)
    if key in seen:
        continue
    seen.add(key)
    # Print layer and brief tab-separated so bash can read them
    print(f"{layer}\t{brief}")
    marker_count += 1

if marker_count:
    sys.exit(0)

# Fallback: preserve meaningful assistant outcomes even when the assistant did
# not follow the explicit marker protocol.  The filter is intentionally
# conservative because Stop fires every turn and must not persist orchestration
# internals or one-word acknowledgements.
CONTROL_LINE_RE = re.compile(
    r"^\s*(?:"
    r"\[crew\]\b|"
    r"STATUS\s*:|PLAN\s*:|BLOCKER\s*:|REVIEW\s*:|ROUTE\s*:|TASK\s*:|"
    r"TASK_ID\s*:|TASK_DIR\s*:|PROJECT_ROOT\s*:|BRANCH\s*:|"
    r"EXECUTION_MODE\s*:|SESSION_ID\s*:|REQUIREMENTS(?:_PATH)?\s*:|"
    r"HANDOFF(?:_PATH)?\s*:|QUALITY_RULE_PATH\s*:|PIPELINE_PATH\s*:|"
    r"AGENT_CREW_HOME\s*:|HOST_TASK_ID\s*:|PROGRESS(?:_LOG)?\s*:|"
    r"<\/?mnemos-[^>]+>|<\/?mnemos-context[^>]*>|<\/?mnemos-capture-protocol>|"
    r"\{TASK_DIR\}|\{PROJECT_ROOT\}|\{BRANCH\}"
    r")",
    re.IGNORECASE,
)
HANDOFF_HINT_RE = re.compile(
    r"\b(?:handoff|downstream agent|stage agent|supervisor pipeline|"
    r"agent-crew task|pipeline\.json|approval\.md|progress\.log)\b",
    re.IGNORECASE,
)
TRIVIAL_RE = re.compile(
    r"^(?:yes|yep|yeah|no|nope|ok|okay|sure|done|thanks|thank you|"
    r"completed|continue|proceed|go|좋아요|네|아니요|완료|진행)$",
    re.IGNORECASE,
)

def clean_lines(text):
    lines = []
    dropped = 0
    for raw in mask_code(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        if CONTROL_LINE_RE.search(line):
            dropped += 1
            continue
        if line.startswith(("<!--", "-->", "|---", "---")):
            dropped += 1
            continue
        if re.fullmatch(r"[\-*_#\s]+", line):
            dropped += 1
            continue
        # Drop markdown table rows and list entries that are just file paths or
        # state coordinates; they are usually handoff/control material.
        if line.startswith("|") and line.endswith("|"):
            dropped += 1
            continue
        if re.fullmatch(r"[-*]\s+(?:/|~|\$|\{).+", line):
            dropped += 1
            continue
        lines.append(line)
    return lines, dropped

def is_trivial(text):
    folded = re.sub(r"\s+", " ", text).strip(" .!?,:;\"'`").lower()
    if not folded:
        return True
    if TRIVIAL_RE.fullmatch(folded):
        return True
    words = re.findall(r"[A-Za-z0-9가-힣_/-]+", folded)
    return len(folded) < 50 or len(words) < 8

def looks_internal(original, lines, dropped):
    if not lines:
        return True
    control_density = dropped / max(dropped + len(lines), 1)
    if control_density >= 0.45 and HANDOFF_HINT_RE.search(original):
        return True
    if HANDOFF_HINT_RE.search(original) and any(
        token in original for token in ("TASK_ID:", "TASK_DIR:", "STATUS:", "PLAN:", "BLOCKER:")
    ):
        return True
    return False

def summarize(lines):
    kept = []
    for line in lines:
        # Keep headings only when followed by detail; standalone headings are
        # not useful memory content.
        if re.fullmatch(r"#{1,6}\s+\S.{0,60}", line):
            continue
        kept.append(re.sub(r"\s+", " ", line))
        if len(" ".join(kept)) >= 360 or len(kept) >= 5:
            break
    summary = " ".join(kept).strip()
    if len(summary) > 500:
        summary = summary[:497].rstrip() + "..."
    return summary

for text in reversed(texts):
    lines, dropped = clean_lines(text)
    if looks_internal(text, lines, dropped):
        continue
    summary = summarize(lines)
    if is_trivial(summary):
        continue
    print(f"session\tAI conversation insight: {summary}")
    break
PYEOF
)"

if [ -z "${MARKERS}" ]; then
  exit 0
fi

# ---------------------------------------------------------------------------
# Capture each parsed marker
# ---------------------------------------------------------------------------
while IFS=$'\t' read -r LAYER BRIEF; do
  [ -z "${BRIEF}" ] && continue
  _timeout "${TIMEOUT}" \
    env MNEMOS_REPO_ROOT="${REPO_ROOT}" \
    mnemos capture \
      --content "${BRIEF}" \
      --quiet \
      --layer "${LAYER}" \
      --session-id "${SESSION_ID}" \
    2>/dev/null || true
done <<< "${MARKERS}"

exit 0
