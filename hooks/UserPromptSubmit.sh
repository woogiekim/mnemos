#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook for mnemos deterministic V1 context injection.

set -euo pipefail

REPO_ROOT="${MNEMOS_REPO_ROOT:-}"
CONTEXT_CACHE_TTL="${MNEMOS_CONTEXT_CACHE_TTL_SECONDS:-300}"
CONTEXT_PREFETCH="${MNEMOS_CONTEXT_PREFETCH:-1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHONPATH_VALUE="${SOURCE_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

[ -n "${REPO_ROOT}" ] || exit 0

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

CACHE_PATHS="$(
  python3 - "${REPO_ROOT}" "${SESSION_ID}" "${PROMPT}" <<'PY' 2>/dev/null || true
import hashlib
import os
import sys
from pathlib import Path

repo_root, session_id, prompt = sys.argv[1:4]
override = os.environ.get("MNEMOS_CONTEXT_CACHE_FILE")
cache_dir = Path(os.environ.get("MNEMOS_CONTEXT_CACHE_DIR") or Path(os.environ.get("TMPDIR", "/tmp")) / "mnemos-context-cache")
session_key = hashlib.sha256(f"{repo_root}\0{session_id}".encode("utf-8")).hexdigest()
prompt_key = hashlib.sha256(f"{repo_root}\0{session_id}\0{prompt}".encode("utf-8")).hexdigest()
exact = Path(override) if override else cache_dir / "exact" / f"{prompt_key}.txt"
last = cache_dir / "session" / f"{session_key}.txt"
print(str(exact))
print(str(last))
PY
)"
EXACT_CACHE="$(printf '%s\n' "${CACHE_PATHS}" | sed -n '1p')"
LAST_CACHE="$(printf '%s\n' "${CACHE_PATHS}" | sed -n '2p')"

is_fresh_cache() {
  local path="$1"
  [ -n "${path}" ] || return 1
  python3 - "${CONTEXT_CACHE_TTL}" "${path}" <<'PY' 2>/dev/null
import sys
import time
from pathlib import Path

ttl = int(float(sys.argv[1]))
path = Path(sys.argv[2])
if not path.is_file():
    raise SystemExit(1)
if ttl >= 0 and time.time() - path.stat().st_mtime > ttl:
    raise SystemExit(1)
raise SystemExit(0)
PY
}

if is_fresh_cache "${EXACT_CACHE}"; then
  cat "${EXACT_CACHE}" 2>/dev/null || true
elif is_fresh_cache "${LAST_CACHE}"; then
  cat "${LAST_CACHE}" 2>/dev/null || true
fi

PROMOTION_BLOCK="$(
  env PYTHONPATH="${PYTHONPATH_VALUE}" python3 - "${REPO_ROOT}" <<'PY' 2>/dev/null || true
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

repo_root = Path(sys.argv[1])
obs_log = repo_root / ".agent" / "observability.jsonl"
if not obs_log.exists():
    raise SystemExit(0)

cursor_path = Path(os.environ.get("MNEMOS_PROMO_CURSOR") or Path.home() / ".mnemos" / ".cache" / "promotion-cursor.txt")
cursor_ts = "2020-01-01T00:00:00Z"
if cursor_path.exists():
    cursor_ts = cursor_path.read_text(encoding="utf-8").strip() or cursor_ts

promotions = []
for raw in obs_log.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        continue
    if entry.get("event") != "promotion":
        continue
    ts = str(entry.get("ts") or "")
    if ts <= cursor_ts:
        continue
    memory_id = entry.get("memory_id") or entry.get("item_id")
    layer = entry.get("layer") or entry.get("to_layer")
    if memory_id and layer:
        promotions.append(f"{memory_id} → {layer}")

if not promotions:
    raise SystemExit(0)

try:
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8")
except OSError:
    pass

print("<mnemos-promotion>")
for item in promotions:
    print(f"<promotion>{html.escape(item)}</promotion>")
print("</mnemos-promotion>")
PY
)"
[ -n "${PROMOTION_BLOCK}" ] && echo "${PROMOTION_BLOCK}"

if [ "${CONTEXT_PREFETCH}" != "0" ] && command -v mnemos >/dev/null 2>&1 && [ -n "${EXACT_CACHE}" ] && [ -n "${LAST_CACHE}" ]; then
  nohup env PYTHONPATH="${PYTHONPATH_VALUE}" python3 "${SCRIPT_DIR}/context_prefetch_worker.py" \
    --repo-root "${REPO_ROOT}" \
    --prompt "${PROMPT}" \
    --session-id "${SESSION_ID}" \
    --host "claude-code" \
    --exact-cache "${EXACT_CACHE}" \
    --last-cache "${LAST_CACHE}" \
    >/dev/null 2>&1 &
fi

exit 0
