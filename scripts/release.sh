#!/usr/bin/env bash
# release.sh — mnemos release pre-flight + command printer (issue #72)
#
# This helper is DRY-RUN BY DEFAULT. With no flags (or with --dry-run) it
# performs read-only checks and PRINTS the tag/build/release commands you
# would run — it never mutates the repository or the network.
#
# It lives OUTSIDE the coverage-gated source tree (core/, agents/) on
# purpose: it is operator tooling, not product code, so it does not count
# against the 100% --cov-fail-under gate. See docs/release-workflow.md.
#
# Hard guarantees (true in every mode):
#   * NEVER runs `git push`.
#   * NEVER runs `python -m build`/`twine`/`pipx publish` against any index.
#   * `--dry-run` (the default) creates NO git tag.
#   * `--execute` may create a LOCAL annotated tag and a LOCAL build artifact
#     only; it still NEVER pushes and NEVER publishes. Releasing to GitHub is
#     a deliberate, separate, human step (`git push --tags` + GitHub Release).
#
# Usage:
#   scripts/release.sh                 # dry-run (default): checks + prints commands
#   scripts/release.sh --dry-run       # explicit dry-run, identical to above
#   scripts/release.sh --execute       # local-only build + annotated tag (no push/publish)
#   scripts/release.sh --help          # show this help and exit
#
set -euo pipefail

MODE="dry-run"
RELEASE_BRANCH_PREFIX="${MNEMOS_RELEASE_BRANCH_PREFIX:-release/}"

usage() {
  sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) MODE="dry-run" ;;
    --execute) MODE="execute" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "release.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# Resolve the repo root from this script's location so the helper works no
# matter the caller's CWD.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

note()  { printf '  %s\n' "$*"; }
ok()    { printf 'OK    %s\n' "$*"; }
warn()  { printf 'WARN  %s\n' "$*"; }
fail()  { printf 'FAIL  %s\n' "$*" >&2; }

echo "mnemos release pre-flight (mode: ${MODE})"
echo "repo: ${REPO_ROOT}"
echo

# ---------------------------------------------------------------------------
# 1. Read the single SemVer source of truth: pyproject.toml::version.
# ---------------------------------------------------------------------------
PKG_VERSION="$(
  python3 - <<'PY'
import re, sys, pathlib
text = pathlib.Path("pyproject.toml").read_text(encoding="utf-8")
m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
sys.stdout.write(m.group(1) if m else "")
PY
)"
if [ -z "$PKG_VERSION" ]; then
  fail "could not read version from pyproject.toml"
  exit 1
fi
ok "pyproject.toml version = ${PKG_VERSION}"
TAG="v${PKG_VERSION}"

# ---------------------------------------------------------------------------
# 2. CHANGELOG consistency: the version must have a section in CHANGELOG.md.
# ---------------------------------------------------------------------------
if [ -f CHANGELOG.md ]; then
  if grep -qE "^##[[:space:]]+\[?${PKG_VERSION}\]?" CHANGELOG.md; then
    ok "CHANGELOG.md has a section for ${PKG_VERSION}"
  else
    warn "CHANGELOG.md has no '## [${PKG_VERSION}]' section — cut it before tagging"
  fi
else
  warn "CHANGELOG.md not found"
fi

# ---------------------------------------------------------------------------
# 3. Working tree must be clean.
# ---------------------------------------------------------------------------
if [ -z "$(git status --porcelain)" ]; then
  ok "working tree is clean"
else
  warn "working tree has uncommitted changes — commit or stash before releasing"
fi

# ---------------------------------------------------------------------------
# 4. Branch check — releases are cut from a release/* branch (or main).
# ---------------------------------------------------------------------------
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "$CURRENT_BRANCH" in
  main|"${RELEASE_BRANCH_PREFIX}"*) ok "on release-capable branch '${CURRENT_BRANCH}'" ;;
  *) warn "on branch '${CURRENT_BRANCH}' — release from 'main' or a '${RELEASE_BRANCH_PREFIX}*' branch" ;;
esac

# ---------------------------------------------------------------------------
# 5. Tag must not already exist locally.
# ---------------------------------------------------------------------------
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null 2>&1; then
  warn "tag ${TAG} already exists locally — bump the version or remove the stale tag"
else
  ok "tag ${TAG} is available"
fi

echo
echo "Test gate (run this yourself in the activated venv before tagging):"
note "source .venv/bin/activate && python -m pytest"
note "# Must pass at 100% coverage with filterwarnings=error (see pyproject.toml)."

echo
echo "Planned release commands for ${TAG}:"
note "git tag -a ${TAG} -m \"mnemos ${TAG}\"   # annotated tag (LOCAL)"
note "python -m build                          # build wheel + sdist into dist/ (LOCAL artifact)"
note "git push origin ${CURRENT_BRANCH}        # human step — NOT run by this script"
note "git push origin ${TAG}                   # human step — NOT run by this script"
note "gh release create ${TAG} dist/* --notes-from-tag  # human step — NOT run by this script"

echo
if [ "$MODE" = "dry-run" ]; then
  echo "DRY-RUN: no tag created, no build run, nothing pushed or published."
  echo "Re-run with --execute to build + create the LOCAL tag (still no push/publish)."
  exit 0
fi

# ---------------------------------------------------------------------------
# --execute: LOCAL-ONLY build + annotated tag. Never pushes, never publishes.
# ---------------------------------------------------------------------------
echo "EXECUTE: building artifact and creating LOCAL annotated tag (no push, no publish)."
python -m build
git tag -a "${TAG}" -m "mnemos ${TAG}"
echo
ok "created LOCAL tag ${TAG} and built dist/ artifacts"
echo "Next (human steps, intentionally NOT automated): push the branch + tag,"
echo "then create the GitHub Release and attach dist/* artifacts."
