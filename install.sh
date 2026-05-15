#!/usr/bin/env bash
# install.sh — one-touch mnemos installer (pipx edition)
# Usage (local):  ./install.sh
# Usage (remote): curl -s https://raw.githubusercontent.com/woogiekim/mnemos/main/install.sh | bash
# No manual venv activation required — pipx handles PATH registration automatically.
set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Find a suitable Python interpreter (>= 3.11)
# ---------------------------------------------------------------------------
find_python() {
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" &>/dev/null; then
            local ver
            ver=$("$candidate" -c "import sys; print(sys.version_info >= (3, 11))" 2>/dev/null || echo "False")
            if [ "$ver" = "True" ]; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    # Also try the codex runtime Python if present
    local codex_py="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12"
    if [ -x "$codex_py" ]; then
        echo "$codex_py"
        return 0
    fi
    return 1
}

PYTHON=$(find_python) || {
    echo "error: Python 3.11+ not found. Install it before running this script." >&2
    exit 1
}
echo "Using Python: $PYTHON"

# ---------------------------------------------------------------------------
# 2. Ensure pipx is installed
# ---------------------------------------------------------------------------
if ! command -v pipx &>/dev/null; then
    echo "pipx not found — installing ..."
    if "$PYTHON" -m pip install --user pipx 2>/dev/null; then
        echo "pipx installed via pip."
    elif command -v brew &>/dev/null; then
        echo "pip install failed; trying brew install pipx ..."
        brew install pipx
    else
        echo "error: Could not install pipx. Install it manually: https://pipx.pypa.io/stable/installation/" >&2
        exit 1
    fi
    # Make sure pipx-managed binaries are on PATH for this session
    "$PYTHON" -m pipx ensurepath 2>/dev/null || true
    export PATH="$HOME/.local/bin:$PATH"
fi

# ---------------------------------------------------------------------------
# 3. Detect execution mode
#    When piped via curl, BASH_SOURCE[0] is empty or equals "bash".
#    When run locally,  BASH_SOURCE[0] is the real path to this file.
# ---------------------------------------------------------------------------
_src="${BASH_SOURCE[0]:-}"
if [ -z "$_src" ] || [ "$_src" = "bash" ]; then
    # ---- Mode 1: curl | bash -----------------------------------------------
    MNEMOS_REMOTE="https://github.com/woogiekim/mnemos.git"
    MNEMOS_DIR="$HOME/.mnemos"

    if [ -d "$MNEMOS_DIR/.git" ]; then
        echo "Updating existing mnemos clone at $MNEMOS_DIR ..."
        git -C "$MNEMOS_DIR" pull origin main
    else
        echo "Cloning mnemos into $MNEMOS_DIR ..."
        git clone "$MNEMOS_REMOTE" "$MNEMOS_DIR"
    fi

    REPO_ROOT="$MNEMOS_DIR"
else
    # ---- Mode 2: ./install.sh (local) --------------------------------------
    REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

# ---------------------------------------------------------------------------
# 4. Install (or reinstall) mnemos via pipx
# ---------------------------------------------------------------------------
if pipx list --short 2>/dev/null | grep -q "^mnemos "; then
    echo "mnemos already installed via pipx — reinstalling from $REPO_ROOT ..."
    pipx reinstall mnemos
else
    echo "Installing mnemos via pipx from $REPO_ROOT ..."
    pipx install -e "$REPO_ROOT"
fi

# ---------------------------------------------------------------------------
# 5. Scaffold the wiki repo structure
# ---------------------------------------------------------------------------
echo "Scaffolding mnemos wiki structure at $REPO_ROOT ..."
mnemos install "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 6. Done
# ---------------------------------------------------------------------------
echo ""
echo "mnemos is now available. Try: mnemos --help"
