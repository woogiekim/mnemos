"""mnemos update — self-update logic.

Sequence:
  1. git pull origin main  (in the mnemos source repo)
  2. pipx reinstall mnemos
  3. Replace managed blocks in host config files, printing a before/after diff.

Managed block sentinels
-----------------------
settings.json  : hook entries containing "mnemos ingest-claude-md"
                 or "mnemos search" (PostToolUse / UserPromptSubmit hooks)
CLAUDE.md      : <!-- mnemos-start --> … <!-- mnemos-end -->
cursor rules   : <!-- mnemos:start --> … <!-- mnemos:end -->
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Canonical managed block content (mirrors install.sh)
# ---------------------------------------------------------------------------

CLAUDE_MD_BLOCK = """\
<!-- mnemos-start -->
## Memory (mnemos)
When asked about past context, notes, or decisions, run:
`mnemos search <query>`
This searches your personal memory store managed by mnemos.

### Capturing knowledge
Use `mnemos capture "<insight>"` when you identify:
- Stable project decisions
- Architecture constraints
- User preferences
- Reusable workflows
- Important operational knowledge
- Persistent project context

Do NOT capture:
- Temporary debugging, speculative reasoning, incomplete thoughts
- Transient conversation, low-confidence assumptions, scratch work

All captured insights begin in the session layer.
Promotion to project or global layer happens through mnemos promotion rules only.
<!-- mnemos-end -->"""

CURSOR_RULES_BLOCK = """\
<!-- mnemos:start -->
## Memory (mnemos)
For memory-related, project-history, architecture-history, or prior-decision questions, run:
`mnemos search <query>`
before answering. Do not assume the current conversation contains complete context.
Use mnemos as the persistent memory retrieval system.
<!-- mnemos:end -->"""

# Hook definitions that install.sh injects.  The MNEMOS_REPO_ROOT placeholder
# is expanded at install time; on update we keep whatever value is already
# recorded in the existing hook command (we preserve the path, only the
# structure/matcher is normalised).
_POST_TOOL_USE_HOOK_TEMPLATE = {
    "matcher": "Write|Edit",
    "hooks": [
        {
            "type": "command",
            "command": "MNEMOS_REPO_ROOT=\"{repo_root}\" mnemos ingest-claude-md",
        }
    ],
}

_USER_PROMPT_SUBMIT_HOOK_TEMPLATE = {
    "matcher": "",
    "hooks": [
        {
            "type": "command",
            "command": (
                'MNEMOS_REPO_ROOT="{repo_root}" mnemos search '
                '"${CLAUDE_PROMPT:0:200}" 2>/dev/null | head -30 || true'
            ),
        }
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unified_diff(label: str, before: str, after: str) -> str:
    """Return a unified-diff string (or empty string if unchanged)."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"{label} (before)",
            tofile=f"{label} (after)",
        )
    )
    return "".join(diff_lines)


def _run(cmd: list[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a subprocess, streaming output to stdout/stderr."""
    return subprocess.run(cmd, cwd=cwd, check=True)


# ---------------------------------------------------------------------------
# Step 1 + 2 — git pull + pipx reinstall
# ---------------------------------------------------------------------------

def git_pull(repo_root: str) -> None:
    """Run git pull origin main in repo_root."""
    _run(["git", "pull", "origin", "main"], cwd=repo_root)


def pipx_reinstall() -> None:
    """Run pipx reinstall mnemos."""
    _run(["pipx", "reinstall", "mnemos"])


# ---------------------------------------------------------------------------
# Step 3a — ~/.claude/settings.json  (hook replacement)
# ---------------------------------------------------------------------------

def _is_mnemos_hook_entry(entry: dict) -> bool:
    """Return True if this hook-list entry contains any mnemos command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if "mnemos ingest-claude-md" in cmd or "mnemos search" in cmd:
            return True
    return False


def _extract_repo_root_from_hook(entry: dict) -> str:
    """Try to extract the MNEMOS_REPO_ROOT value from an existing hook command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        m = re.search(r'MNEMOS_REPO_ROOT="([^"]+)"', cmd)
        if m:
            return m.group(1)
    return os.environ.get("MNEMOS_REPO_ROOT", "")


def update_settings_json(settings_path: Path) -> tuple[bool, str]:
    """Replace mnemos hook entries in settings.json with canonical versions.

    Returns (changed, diff_text).
    """
    if not settings_path.exists():
        return False, ""

    original_text = settings_path.read_text()
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Cannot parse {settings_path}: {exc}") from exc

    hooks = data.get("hooks", {})
    changed = False

    # Collect repo_root from existing entries (prefer first found)
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    for hook_type in ("PostToolUse", "UserPromptSubmit"):
        for entry in hooks.get(hook_type, []):
            if _is_mnemos_hook_entry(entry):
                found = _extract_repo_root_from_hook(entry)
                if found:
                    repo_root = found
                    break
        if repo_root:
            break

    # Remove all existing mnemos hook entries and replace with canonical ones
    for hook_type, template in [
        ("PostToolUse", _POST_TOOL_USE_HOOK_TEMPLATE),
        ("UserPromptSubmit", _USER_PROMPT_SUBMIT_HOOK_TEMPLATE),
    ]:
        hook_list = hooks.get(hook_type, [])
        # Strip old mnemos entries
        non_mnemos = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        # Build canonical replacement entry (fill in repo_root)
        canonical = json.loads(
            json.dumps(template).replace("{repo_root}", repo_root)
        )
        new_list = non_mnemos + [canonical]
        if new_list != hook_list:
            changed = True
        hooks[hook_type] = new_list

    if not changed:
        return False, ""

    data["hooks"] = hooks
    new_text = json.dumps(data, indent=2) + "\n"
    diff = _unified_diff(str(settings_path), original_text, new_text)
    settings_path.write_text(new_text)
    return True, diff


# ---------------------------------------------------------------------------
# Step 3b — ~/.claude/CLAUDE.md
# ---------------------------------------------------------------------------

def update_claude_md(claude_md_path: Path) -> tuple[bool, str]:
    """Replace <!-- mnemos-start --> … <!-- mnemos-end --> block.

    Returns (changed, diff_text).
    """
    if not claude_md_path.exists():
        return False, ""

    original = claude_md_path.read_text()
    pattern = re.compile(
        r"<!-- mnemos-start -->.*?<!-- mnemos-end -->",
        re.DOTALL,
    )

    if pattern.search(original):
        updated = pattern.sub(CLAUDE_MD_BLOCK, original)
    else:
        # Block not present — append it
        updated = original.rstrip("\n") + "\n\n" + CLAUDE_MD_BLOCK + "\n"

    if updated == original:
        return False, ""

    diff = _unified_diff(str(claude_md_path), original, updated)
    claude_md_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# Step 3c — ~/.cursor/rules
# ---------------------------------------------------------------------------

def _find_cursor_rules(cursor_dir: Path) -> Optional[Path]:
    """Return ~/.cursor/rules, ~/.cursor/rules.md, or None."""
    for name in ("rules", "rules.md"):
        p = cursor_dir / name
        if p.exists():
            return p
    return None


def update_cursor_rules(cursor_dir: Path) -> tuple[bool, str]:
    """Replace <!-- mnemos:start --> … <!-- mnemos:end --> block.

    Returns (changed, diff_text).
    """
    rules_path = _find_cursor_rules(cursor_dir)
    if rules_path is None:
        return False, ""

    original = rules_path.read_text()
    pattern = re.compile(
        r"<!-- mnemos:start -->.*?<!-- mnemos:end -->",
        re.DOTALL,
    )

    if pattern.search(original):
        updated = pattern.sub(CURSOR_RULES_BLOCK, original)
    else:
        # Block not present — append it
        updated = original.rstrip("\n") + "\n\n" + CURSOR_RULES_BLOCK + "\n"

    if updated == original:
        return False, ""

    diff = _unified_diff(str(rules_path), original, updated)
    rules_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_update(
    repo_root: Optional[str] = None,
    skip_git_pull: bool = False,
    skip_pipx: bool = False,
    home: Optional[Path] = None,
) -> int:
    """Run the full update sequence.  Returns exit code (0 = success)."""
    if home is None:
        home = Path.home()

    # Resolve repo_root
    if repo_root is None:
        repo_root = os.environ.get("MNEMOS_REPO_ROOT")
    if not repo_root:
        # Attempt to find it from pipx venv
        repo_root = str(Path(__file__).resolve().parents[1])

    exit_code = 0

    # -- 1. git pull ---------------------------------------------------------
    if not skip_git_pull:
        print("── git pull origin main ──────────────────────────────────────")
        try:
            git_pull(repo_root)
        except subprocess.CalledProcessError as exc:
            print(f"warning: git pull failed — {exc}", file=sys.stderr)
            exit_code = 1

    # -- 2. pipx reinstall ---------------------------------------------------
    if not skip_pipx:
        print("\n── pipx reinstall mnemos ─────────────────────────────────────")
        try:
            pipx_reinstall()
        except subprocess.CalledProcessError as exc:
            print(f"warning: pipx reinstall failed — {exc}", file=sys.stderr)
            exit_code = 1

    # -- 3. Replace managed blocks ------------------------------------------
    print("\n── updating managed config blocks ───────────────────────────────")

    settings_path = home / ".claude" / "settings.json"
    changed, diff = update_settings_json(settings_path)
    if changed:
        print(f"\n[updated] {settings_path}")
        if diff:
            print(diff)
    else:
        print(f"[unchanged] {settings_path}")

    claude_md_path = home / ".claude" / "CLAUDE.md"
    changed, diff = update_claude_md(claude_md_path)
    if changed:
        print(f"\n[updated] {claude_md_path}")
        if diff:
            print(diff)
    else:
        print(f"[unchanged] {claude_md_path}")

    cursor_dir = home / ".cursor"
    changed, diff = update_cursor_rules(cursor_dir)
    if changed:
        rules_path = _find_cursor_rules(cursor_dir)
        label = str(rules_path) if rules_path else str(cursor_dir / "rules")
        print(f"\n[updated] {label}")
        if diff:
            print(diff)
    else:
        print(f"[unchanged] {cursor_dir / 'rules'}")

    print("\n── update complete ───────────────────────────────────────────────")
    return exit_code
