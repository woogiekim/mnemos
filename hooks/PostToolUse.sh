#!/usr/bin/env bash
# Claude Code PostToolUse hook for throttled mnemos maintenance.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
exec python3 "${SCRIPT_DIR}/post_tool_use.py"
