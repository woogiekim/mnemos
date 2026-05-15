#!/usr/bin/env bash
# install.sh — one-touch mnemos installer
# Usage (local):  ./install.sh
# Usage (remote): curl -s https://raw.githubusercontent.com/woogiekim/mnemos/main/install.sh | bash
# No manual venv activation or pip install required.
set -euo pipefail

# ---------------------------------------------------------------------------
# 0. Detect execution mode
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
# 1. Find a suitable Python interpreter (>= 3.11)
# ---------------------------------------------------------------------------
find_python() {
    # Prefer explicit version binaries first, then fall back to python3
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

# ---------------------------------------------------------------------------
# 2. Determine which pip / mnemos binary to use
# ---------------------------------------------------------------------------
if [ -n "${VIRTUAL_ENV:-}" ]; then
    # A venv is already active — honour it
    PIP="$VIRTUAL_ENV/bin/pip"
    MNEMOS_BIN="$VIRTUAL_ENV/bin/mnemos"
    echo "Using active venv: $VIRTUAL_ENV"
else
    VENV_DIR="$REPO_ROOT/.venv"
    if [ ! -d "$VENV_DIR" ]; then
        PYTHON=$(find_python) || {
            echo "error: Python 3.11+ not found. Install it before running this script." >&2
            exit 1
        }
        echo "Creating virtual environment at $VENV_DIR (using $PYTHON) ..."
        "$PYTHON" -m venv "$VENV_DIR"
    else
        echo "Reusing existing virtual environment at $VENV_DIR"
    fi
    PIP="$VENV_DIR/bin/pip"
    MNEMOS_BIN="$VENV_DIR/bin/mnemos"
fi

# ---------------------------------------------------------------------------
# 3. Install the package
# ---------------------------------------------------------------------------
echo "Upgrading pip ..."
"$PIP" install --quiet --upgrade pip

echo "Installing mnemos package ..."
"$PIP" install -e "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 4. Scaffold the wiki repo structure
# ---------------------------------------------------------------------------
echo "Scaffolding mnemos wiki structure at $REPO_ROOT ..."
"$MNEMOS_BIN" install "$REPO_ROOT"

# ---------------------------------------------------------------------------
# 5. Done
# ---------------------------------------------------------------------------
echo ""
echo "mnemos installation complete."
if [ -z "${VIRTUAL_ENV:-}" ]; then
    echo ""
    echo "To activate the virtual environment, run:"
    echo "  source \"$REPO_ROOT/.venv/bin/activate\""
fi
