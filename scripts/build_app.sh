#!/usr/bin/env bash
# build_app.sh — assemble the mnemos macOS .app bundle (issue #94)
#
# This helper is DRY-RUN BY DEFAULT. With no flags (or with --dry-run) it
# performs no mutation and PRINTS the pyinstaller command that --execute would
# run. With --execute it invokes pyinstaller against app/mnemos_app.spec,
# verifies the assembled bundle structure, and prints the final .app path +
# size.
#
# It lives OUTSIDE the coverage-gated source tree (core/, agents/) on purpose:
# it is operator tooling, not product code, so it does not count against the
# 100% --cov-fail-under gate. See docs/desktop-app.md.
#
# Hard guarantees (true in every mode):
#   * NEVER runs codesign / spctl / notarytool / xcrun-anything.
#   * NEVER runs `git push` or `git tag` (release.sh owns release flow, #72).
#   * --dry-run (the default) writes nothing to dist/ and runs no pyinstaller.
#   * --execute writes ONLY to dist/ and build/ (local artifacts only). The
#     bundle is unsigned and will trigger Gatekeeper on first launch — that
#     bypass is a deliberate, separate, human step (see docs/desktop-app.md).
#
# Usage:
#   scripts/build_app.sh                # dry-run (default): prints planned command
#   scripts/build_app.sh --dry-run      # explicit dry-run, identical to above
#   scripts/build_app.sh --execute      # build + verify the local .app bundle
#   scripts/build_app.sh --help         # show this help and exit
#
set -euo pipefail

MODE="dry-run"

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --execute) MODE="execute" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "build_app.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# Resolve the repo root from this script's location so the helper works no
# matter the caller's CWD (mirrors scripts/release.sh:1-146).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

note() { printf '  %s\n' "$*"; }
ok()   { printf 'OK    %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*" >&2; }

echo "mnemos .app build (mode: ${MODE})"
echo "repo: ${REPO_ROOT}"
echo

SPEC="${REPO_ROOT}/app/mnemos_app.spec"
if [ ! -f "$SPEC" ]; then
  fail "spec not found: ${SPEC}"
  exit 1
fi
ok "spec found at app/mnemos_app.spec"

PLANNED_CMD="pyinstaller --noconfirm app/mnemos_app.spec"

echo
echo "Planned command:"
note "${PLANNED_CMD}"

if [ "$MODE" = "dry-run" ]; then
  echo
  echo "DRY-RUN: no bundle built. Re-run with --execute to assemble dist/mnemos.app."
  exit 0
fi

# ---------------------------------------------------------------------------
# --execute: invoke pyinstaller and verify the assembled bundle.
# ---------------------------------------------------------------------------
echo
echo "EXECUTE: building dist/mnemos.app (unsigned, local-only)."
pyinstaller --noconfirm app/mnemos_app.spec

APP_PATH="${REPO_ROOT}/dist/mnemos.app"
INFO_PLIST="${APP_PATH}/Contents/Info.plist"
BINARY="${APP_PATH}/Contents/MacOS/mnemos"
UI_TEMPLATE="${APP_PATH}/Contents/Resources/core/templates/ui.html"

# File-existence guards mirror the pattern at scripts/release.sh:1-146:
# verify each load-bearing file individually so a missing piece names itself.
for path in "${APP_PATH}" "${INFO_PLIST}" "${BINARY}" "${UI_TEMPLATE}"; do
  if [ ! -e "$path" ]; then
    fail "missing expected artifact: ${path}"
    exit 1
  fi
done

ok "dist/mnemos.app/Contents/Info.plist present"
ok "dist/mnemos.app/Contents/MacOS/mnemos present"
ok "dist/mnemos.app/Contents/Resources/core/templates/ui.html present"

# Report the bundle size for operator sanity. `du -sh` is portable across
# macOS and Linux; we deliberately do not parse it programmatically.
APP_SIZE="$(du -sh "${APP_PATH}" | awk '{print $1}')"

echo
ok "built ${APP_PATH} (${APP_SIZE})"
echo
echo "Next (manual steps — intentionally NOT automated):"
note "open ${APP_PATH}                                # first-launch GUI verification"
note "MNEMOS_APP_HEADLESS=1 ${BINARY}                 # headless smoke (no GUI)"
note "# macOS Gatekeeper: right-click → Open the first time the bundle is unsigned."
