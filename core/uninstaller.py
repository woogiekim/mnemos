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
import shutil
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
# pipx uninstall + fallback cleanup
# ---------------------------------------------------------------------------

def _default_pipx_bin_dir() -> Path:
    """Return the default pipx binary directory (~/.local/bin)."""
    return Path.home() / ".local" / "bin"


def _default_pipx_venvs_dir() -> Path:
    """Return the default pipx venvs directory (~/.local/pipx/venvs)."""
    return Path.home() / ".local" / "pipx" / "venvs"


def _read_pipx_metadata(venvs_dir: Optional[Path] = None) -> Optional[dict]:
    """Read ~/.local/pipx/venvs/mnemos/pipx_metadata.json if it exists.

    Returns the parsed JSON dict or None if not found / unreadable.
    """
    if venvs_dir is None:
        venvs_dir = _default_pipx_venvs_dir()
    metadata_path = venvs_dir / "mnemos" / "pipx_metadata.json"
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def pipx_uninstall() -> None:
    """Run pipx uninstall mnemos."""
    subprocess.run(["pipx", "uninstall", "mnemos"], check=True)


def purge_mnemos_artifacts(
    bin_dir: Optional[Path] = None,
    venvs_dir: Optional[Path] = None,
) -> dict[str, object]:
    """Remove the mnemos binary and venv directory with fallback cleanup.

    Steps:
      1. Read pipx_metadata.json to find the actual install location.
      2. Try ``pipx uninstall mnemos`` (existing behaviour).
      3. Fallback: directly remove the binary file if still present.
      4. Fallback: directly shutil.rmtree the venv directory if still present.
      5. Verify with shutil.which("mnemos") that the binary is gone.

    Returns a dict with keys:
      ``pipx_ok``       — bool, True if pipx uninstall succeeded
      ``binary_removed`` — Path or None, the binary that was removed by fallback
      ``venv_removed``   — Path or None, the venv dir that was removed by fallback
      ``metadata``       — dict or None, content of pipx_metadata.json (may be None)
      ``still_present``  — bool, True if shutil.which still finds the binary after cleanup
    """
    if bin_dir is None:
        bin_dir = _default_pipx_bin_dir()
    if venvs_dir is None:
        venvs_dir = _default_pipx_venvs_dir()

    binary_path = bin_dir / "mnemos"
    venv_path = venvs_dir / "mnemos"

    # Step 1 — read metadata (informational; used for reporting)
    metadata = _read_pipx_metadata(venvs_dir)

    # Step 2 — try pipx uninstall
    pipx_ok = False
    try:
        subprocess.run(
            ["pipx", "uninstall", "mnemos"],
            check=True,
            capture_output=True,
        )
        pipx_ok = True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pipx_ok = False

    # Step 3 — fallback: remove binary directly
    binary_removed: Optional[Path] = None
    if binary_path.exists():
        try:
            binary_path.unlink()
            binary_removed = binary_path
        except OSError:
            pass

    # Step 4 — fallback: remove venv directory directly
    venv_removed: Optional[Path] = None
    if venv_path.exists():
        try:
            shutil.rmtree(venv_path)
            venv_removed = venv_path
        except OSError:
            pass

    # Step 5 — verify
    still_present = shutil.which("mnemos") is not None

    return {
        "pipx_ok": pipx_ok,
        "binary_removed": binary_removed,
        "venv_removed": venv_removed,
        "metadata": metadata,
        "still_present": still_present,
    }


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

    # Optional pipx uninstall + fallback artifact removal
    if purge:
        print("\n── purge: removing mnemos binary and venv ────────────────────")
        result = purge_mnemos_artifacts()

        # Report metadata location (informational)
        if result["metadata"] is not None:
            print("[info] read pipx_metadata.json — found install metadata")
        else:
            print("[info] pipx_metadata.json not found (package may already be removed)")

        # Report pipx outcome
        if result["pipx_ok"]:
            print("[removed] pipx uninstall mnemos succeeded")
        else:
            print("[warning] pipx uninstall did not succeed — applying fallback cleanup",
                  file=sys.stderr)

        # Report fallback removals
        if result["binary_removed"] is not None:
            print(f"[removed] binary: {result['binary_removed']}")
        if result["venv_removed"] is not None:
            print(f"[removed] venv:   {result['venv_removed']}")

        # Final verification
        if result["still_present"]:
            print(
                "warning: mnemos binary still found in PATH after purge — "
                "manual cleanup may be required",
                file=sys.stderr,
            )
            print("\n── uninstall complete (with warnings) ────────────────────────")
            return 1
        else:
            print("[verified] mnemos binary is no longer in PATH")

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
