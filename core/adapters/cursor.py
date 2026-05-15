"""CursorAdapter — manages mnemos integration with Cursor."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from core.adapters.base import HostAdapter


# ---------------------------------------------------------------------------
# Canonical managed block content (mirrors install.sh)
# ---------------------------------------------------------------------------

CURSOR_RULES_BLOCK = """\
<!-- mnemos:start -->
## Memory (mnemos)
For memory-related, project-history, architecture-history, or prior-decision questions, run:
`mnemos search <query>`
before answering. Do not assume the current conversation contains complete context.
Use mnemos as the persistent memory retrieval system.
<!-- mnemos:end -->"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_cursor_rules(cursor_dir: Path) -> Optional[Path]:
    """Return ~/.cursor/rules, ~/.cursor/rules.md, or None."""
    for name in ("rules", "rules.md"):
        p = cursor_dir / name
        if p.exists():
            return p
    return None


def _unified_diff(label: str, before: str, after: str) -> str:
    import difflib
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


# ---------------------------------------------------------------------------
# CursorAdapter
# ---------------------------------------------------------------------------

class CursorAdapter(HostAdapter):
    """Adapter for Cursor IDE.

    Owns:
    - cursor rules managed block (<!-- mnemos:start --> ... <!-- mnemos:end -->)
      in ~/.cursor/rules or ~/.cursor/rules.md
    """

    @property
    def name(self) -> str:
        return "Cursor"

    def is_present(self, home: Path) -> bool:
        """Return True if ~/.cursor exists."""
        return (home / ".cursor").exists()

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    def install(self, home: Path) -> list[str]:
        """Install the mnemos managed block into cursor rules.

        Returns a list of human-readable status messages.
        """
        cursor_dir = home / ".cursor"
        changed, diff = self._update_cursor_rules(cursor_dir)
        if changed:
            rules_path = _find_cursor_rules(cursor_dir)
            label = str(rules_path) if rules_path else str(cursor_dir / "rules")
            return [f"[installed] managed block in {label}"]
        return [f"[unchanged] {cursor_dir / 'rules'}"]

    # ------------------------------------------------------------------
    # update
    # ------------------------------------------------------------------

    def update(self, home: Path) -> list[str]:
        """Replace the cursor rules managed block with the canonical version.

        Returns a list of human-readable status messages (includes diff output).
        """
        cursor_dir = home / ".cursor"
        changed, diff = self._update_cursor_rules(cursor_dir)
        if changed:
            rules_path = _find_cursor_rules(cursor_dir)
            label = str(rules_path) if rules_path else str(cursor_dir / "rules")
            messages = [f"[updated] {label}"]
            if diff:
                messages.append(diff)
            return messages
        return [f"[unchanged] {cursor_dir / 'rules'}"]

    def _update_cursor_rules(self, cursor_dir: Path) -> tuple[bool, str]:
        """Replace <!-- mnemos:start --> ... <!-- mnemos:end --> block."""
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
            updated = original.rstrip("\n") + "\n\n" + CURSOR_RULES_BLOCK + "\n"

        if updated == original:
            return False, ""

        diff = _unified_diff(str(rules_path), original, updated)
        rules_path.write_text(updated)
        return True, diff

    # ------------------------------------------------------------------
    # uninstall
    # ------------------------------------------------------------------

    def uninstall(self, home: Path) -> list[str]:
        """Remove the cursor rules managed block.

        Returns a list of human-readable status messages.
        """
        cursor_dir = home / ".cursor"
        rules_path = _find_cursor_rules(cursor_dir)
        changed, _ = self._remove_cursor_rules_block(cursor_dir)
        cursor_label = str(rules_path) if rules_path else str(cursor_dir / "rules")
        return [f"[{'removed' if changed else 'unchanged'}] {cursor_label}"]

    def _remove_cursor_rules_block(self, cursor_dir: Path) -> tuple[bool, str]:
        """Remove the <!-- mnemos:start --> ... <!-- mnemos:end --> block."""
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
