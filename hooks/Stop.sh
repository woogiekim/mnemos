#!/usr/bin/env bash
# Claude Code Stop hook for mnemos deterministic transcript capture.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
exec python3 "${SCRIPT_DIR}/stop_hook.py"
