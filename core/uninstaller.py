"""mnemos uninstall — remove all mnemos-managed entries from host config files.

Removal targets:
  1. ~/.claude/settings.json  — PostToolUse and UserPromptSubmit hook entries
                                 containing mnemos commands
  2. ~/.claude/CLAUDE.md      — <!-- mnemos-start --> … <!-- mnemos-end --> block
  3. ~/.cursor/rules or rules.md — <!-- mnemos:start --> … <!-- mnemos:end --> block
  4. ~/.zshrc                 — export MNEMOS_REPO_ROOT=... line (+ optional comment)

Safety: only managed sections are touched; surrounding content is preserved.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unified_diff(label: str, before: str, after: str) -> str:
    """Return a unified-diff string (empty when unchanged)."""
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


def _is_mnemos_hook_entry(entry: dict) -> bool:
    """Return True if this hook-list entry contains a mnemos command."""
    for h in entry.get("hooks", []):
        cmd = h.get("command", "")
        if "mnemos ingest-claude-md" in cmd or "mnemos search" in cmd:
            return True
    return False


# ---------------------------------------------------------------------------
# 1. ~/.claude/settings.json
# ---------------------------------------------------------------------------

def remove_settings_json_hooks(settings_path: Path) -> tuple[bool, str]:
    """Remove all mnemos hook entries from settings.json.

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

    for hook_type in ("PostToolUse", "UserPromptSubmit", "Stop"):
        hook_list = hooks.get(hook_type, [])
        cleaned = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        if cleaned != hook_list:
            changed = True
            if cleaned:
                hooks[hook_type] = cleaned
            else:
                del hooks[hook_type]

    if not changed:
        return False, ""

    if hooks:
        data["hooks"] = hooks
    elif "hooks" in data:
        del data["hooks"]

    new_text = json.dumps(data, indent=2) + "\n"
    diff = _unified_diff(str(settings_path), original_text, new_text)
    settings_path.write_text(new_text)
    return True, diff


# ---------------------------------------------------------------------------
# 2. ~/.claude/CLAUDE.md
# ---------------------------------------------------------------------------

def remove_claude_md_block(claude_md_path: Path) -> tuple[bool, str]:
    """Remove the <!-- mnemos-start --> … <!-- mnemos-end --> block.

    Also removes a leading blank line before the block if present.
    Returns (changed, diff_text).
    """
    if not claude_md_path.exists():
        return False, ""

    original = claude_md_path.read_text()

    # Pattern: optional leading newline(s), the managed block, optional trailing newline
    pattern = re.compile(
        r"\n?<!-- mnemos-start -->.*?<!-- mnemos-end -->\n?",
        re.DOTALL,
    )

    updated = pattern.sub("", original)
    # Normalise multiple trailing newlines down to one
    updated = re.sub(r"\n{3,}", "\n\n", updated)

    if updated == original:
        return False, ""

    diff = _unified_diff(str(claude_md_path), original, updated)
    claude_md_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# 3. ~/.cursor/rules or rules.md
# ---------------------------------------------------------------------------

def _find_cursor_rules(cursor_dir: Path) -> Optional[Path]:
    """Return ~/.cursor/rules, ~/.cursor/rules.md, or None."""
    for name in ("rules", "rules.md"):
        p = cursor_dir / name
        if p.exists():
            return p
    return None


def remove_cursor_rules_block(cursor_dir: Path) -> tuple[bool, str]:
    """Remove the <!-- mnemos:start --> … <!-- mnemos:end --> block.

    Returns (changed, diff_text).
    """
    rules_path = _find_cursor_rules(cursor_dir)
    if rules_path is None:
        return False, ""

    original = rules_path.read_text()

    pattern = re.compile(
        r"\n?<!-- mnemos:start -->.*?<!-- mnemos:end -->\n?",
        re.DOTALL,
    )

    updated = pattern.sub("", original)
    updated = re.sub(r"\n{3,}", "\n\n", updated)

    if updated == original:
        return False, ""

    diff = _unified_diff(str(rules_path), original, updated)
    rules_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# 4. ~/.zshrc
# ---------------------------------------------------------------------------

def remove_zshrc_line(zshrc_path: Path) -> tuple[bool, str]:
    """Remove the export MNEMOS_REPO_ROOT=... line (and preceding comment).

    Recognises lines of the form:
      # mnemos — set MNEMOS_REPO_ROOT  (optional comment line)
      export MNEMOS_REPO_ROOT="..."

    Returns (changed, diff_text).
    """
    if not zshrc_path.exists():
        return False, ""

    original = zshrc_path.read_text()
    lines = original.splitlines(keepends=True)

    # Mark lines for removal
    to_remove: set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"export MNEMOS_REPO_ROOT=", line.strip()):
            to_remove.add(i)
            # Check if the immediately preceding line is a mnemos comment
            if i > 0 and re.match(r"#.*mnemos", lines[i - 1], re.IGNORECASE):
                to_remove.add(i - 1)
        i += 1

    if not to_remove:
        return False, ""

    updated_lines = [line for idx, line in enumerate(lines) if idx not in to_remove]
    updated = "".join(updated_lines)

    diff = _unified_diff(str(zshrc_path), original, updated)
    zshrc_path.write_text(updated)
    return True, diff


# ---------------------------------------------------------------------------
# pipx uninstall
# ---------------------------------------------------------------------------

def pipx_uninstall() -> None:
    """Run pipx uninstall mnemos."""
    subprocess.run(["pipx", "uninstall", "mnemos"], check=True)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_uninstall(
    yes: bool = False,
    purge: bool = False,
    home: Optional[Path] = None,
) -> int:
    """Remove all mnemos-managed config entries.

    Returns exit code (0 = success, 1 = aborted or error).
    """
    if home is None:
        home = Path.home()

    settings_path = home / ".claude" / "settings.json"
    claude_md_path = home / ".claude" / "CLAUDE.md"
    cursor_dir = home / ".cursor"
    zshrc_path = home / ".zshrc"

    # Collect diffs (dry-run pass — do NOT write yet)
    plans: list[tuple[str, callable]] = [
        (str(settings_path), lambda: remove_settings_json_hooks(settings_path)),
        (str(claude_md_path), lambda: remove_claude_md_block(claude_md_path)),
        (str(cursor_dir), lambda: remove_cursor_rules_block(cursor_dir)),
        (str(zshrc_path), lambda: remove_zshrc_line(zshrc_path)),
    ]

    # Preview what would change
    print("── mnemos uninstall — preview ────────────────────────────────────")
    pending: list[tuple[str, str]] = []

    for label, fn in plans:
        # We need to run the function to get the diff, but since these functions
        # write to disk we use a read-only preview approach below.
        pass

    # Read-only diff preview
    diffs_preview = _collect_diffs_preview(
        settings_path, claude_md_path, cursor_dir, zshrc_path
    )

    any_change = False
    for label, diff in diffs_preview:
        if diff:
            any_change = True
            print(f"\n[will remove from] {label}")
            print(diff)
        else:
            print(f"[no managed content] {label}")

    if not any_change:
        print("\nNothing to uninstall — no managed mnemos sections found.")
        return 0

    # Confirmation
    if not yes:
        try:
            answer = input("\nProceed with removal? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    # Apply changes
    print("\n── applying changes ──────────────────────────────────────────────")

    changed_settings, _ = remove_settings_json_hooks(settings_path)
    print(f"[{'removed' if changed_settings else 'unchanged'}] {settings_path}")

    changed_claude_md, _ = remove_claude_md_block(claude_md_path)
    print(f"[{'removed' if changed_claude_md else 'unchanged'}] {claude_md_path}")

    rules_path = _find_cursor_rules(cursor_dir)
    changed_cursor, _ = remove_cursor_rules_block(cursor_dir)
    cursor_label = str(rules_path) if rules_path else str(cursor_dir / "rules")
    print(f"[{'removed' if changed_cursor else 'unchanged'}] {cursor_label}")

    changed_zshrc, _ = remove_zshrc_line(zshrc_path)
    print(f"[{'removed' if changed_zshrc else 'unchanged'}] {zshrc_path}")

    # Optional pipx uninstall
    if purge:
        print("\n── pipx uninstall mnemos ─────────────────────────────────────")
        try:
            pipx_uninstall()
        except subprocess.CalledProcessError as exc:
            print(f"warning: pipx uninstall failed — {exc}", file=sys.stderr)
            print("\n── uninstall complete (with warnings) ────────────────────────")
            return 1

    print("\n── uninstall complete ────────────────────────────────────────────")
    return 0


# ---------------------------------------------------------------------------
# Read-only diff preview helpers
# ---------------------------------------------------------------------------

def _preview_settings_json(settings_path: Path) -> str:
    """Return diff of what would be removed from settings.json (read-only)."""
    if not settings_path.exists():
        return ""

    original_text = settings_path.read_text()
    try:
        data = json.loads(original_text)
    except json.JSONDecodeError:
        return ""

    hooks = data.get("hooks", {})
    changed = False

    for hook_type in ("PostToolUse", "UserPromptSubmit", "Stop"):
        hook_list = hooks.get(hook_type, [])
        cleaned = [e for e in hook_list if not _is_mnemos_hook_entry(e)]
        if cleaned != hook_list:
            changed = True
            if cleaned:
                hooks[hook_type] = cleaned
            else:
                del hooks[hook_type]

    if not changed:
        return ""

    if hooks:
        data["hooks"] = hooks
    elif "hooks" in data:
        del data["hooks"]

    new_text = json.dumps(data, indent=2) + "\n"
    return _unified_diff(str(settings_path), original_text, new_text)


def _preview_claude_md(claude_md_path: Path) -> str:
    """Return diff of what would be removed from CLAUDE.md (read-only)."""
    if not claude_md_path.exists():
        return ""

    original = claude_md_path.read_text()
    pattern = re.compile(
        r"\n?<!-- mnemos-start -->.*?<!-- mnemos-end -->\n?",
        re.DOTALL,
    )
    updated = pattern.sub("", original)
    updated = re.sub(r"\n{3,}", "\n\n", updated)
    if updated == original:
        return ""
    return _unified_diff(str(claude_md_path), original, updated)


def _preview_cursor_rules(cursor_dir: Path) -> str:
    """Return diff of what would be removed from cursor rules (read-only)."""
    rules_path = _find_cursor_rules(cursor_dir)
    if rules_path is None:
        return ""

    original = rules_path.read_text()
    pattern = re.compile(
        r"\n?<!-- mnemos:start -->.*?<!-- mnemos:end -->\n?",
        re.DOTALL,
    )
    updated = pattern.sub("", original)
    updated = re.sub(r"\n{3,}", "\n\n", updated)
    if updated == original:
        return ""
    return _unified_diff(str(rules_path), original, updated)


def _preview_zshrc(zshrc_path: Path) -> str:
    """Return diff of what would be removed from .zshrc (read-only)."""
    if not zshrc_path.exists():
        return ""

    original = zshrc_path.read_text()
    lines = original.splitlines(keepends=True)
    to_remove: set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"export MNEMOS_REPO_ROOT=", line.strip()):
            to_remove.add(i)
            if i > 0 and re.match(r"#.*mnemos", lines[i - 1], re.IGNORECASE):
                to_remove.add(i - 1)
        i += 1

    if not to_remove:
        return ""

    updated_lines = [line for idx, line in enumerate(lines) if idx not in to_remove]
    updated = "".join(updated_lines)
    return _unified_diff(str(zshrc_path), original, updated)


def _collect_diffs_preview(
    settings_path: Path,
    claude_md_path: Path,
    cursor_dir: Path,
    zshrc_path: Path,
) -> list[tuple[str, str]]:
    """Collect read-only diffs for all targets."""
    results = []
    results.append((str(settings_path), _preview_settings_json(settings_path)))
    results.append((str(claude_md_path), _preview_claude_md(claude_md_path)))

    rules_path = _find_cursor_rules(cursor_dir)
    cursor_label = str(rules_path) if rules_path else str(cursor_dir / "rules")
    results.append((cursor_label, _preview_cursor_rules(cursor_dir)))

    results.append((str(zshrc_path), _preview_zshrc(zshrc_path)))
    return results
