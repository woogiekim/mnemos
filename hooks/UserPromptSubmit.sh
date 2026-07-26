#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook for mnemos deterministic V1 context injection.

SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
exec python3 "${SCRIPT_DIR}/user_prompt_submit.py"
