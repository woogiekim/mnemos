"""CodexAdapter - manages mnemos integration with Codex."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from core.adapters.base import HostAdapter, MNEMOS_BEHAVIOR_BLOCK

if TYPE_CHECKING:
    from core.events import EventBus


CODEX_AGENTS_BLOCK = (
    "<!-- mnemos:start -->\n"
    + MNEMOS_BEHAVIOR_BLOCK
    + "\n<!-- mnemos:end -->"
)


def _agents_path(codex_dir: Path) -> Path:
    """Return the Codex AGENTS.md path."""
    return codex_dir / "AGENTS.md"


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


class CodexAdapter(HostAdapter):
    """Adapter for Codex.

    Owns:
    - a mnemos managed block in ``~/.codex/AGENTS.md``

    Not supported:
    - autonomous capture hooks
    - deterministic pre-prompt context injection hooks
    """

    @property
    def name(self) -> str:
        return "Codex"

    def is_present(self, home: Path) -> bool:
        """Return True if ~/.codex exists."""
        return (home / ".codex").exists()

    def install(self, home: Path) -> list[str]:
        """Install the mnemos managed block into Codex AGENTS.md."""
        codex_dir = home / ".codex"
        changed, _ = self._update_agents_md(codex_dir)
        label = str(_agents_path(codex_dir))
        return [f"[{'installed' if changed else 'unchanged'}] managed block in {label}"]

    def update(self, home: Path) -> list[str]:
        """Replace the Codex AGENTS.md managed block with the canonical version."""
        codex_dir = home / ".codex"
        changed, diff = self._update_agents_md(codex_dir)
        label = str(_agents_path(codex_dir))

        if changed:
            messages = [f"[updated] {label}"]
            if diff:
                messages.append(diff)
            return messages

        return [f"[unchanged] {label}"]

    def _update_agents_md(self, codex_dir: Path) -> tuple[bool, str]:
        """Replace or append the <!-- mnemos:start --> managed block."""
        if not codex_dir.exists():
            return False, ""

        agents_path = _agents_path(codex_dir)
        original = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
        pattern = re.compile(
            r"<!-- mnemos:start -->.*?<!-- mnemos:end -->",
            re.DOTALL,
        )

        if pattern.search(original):
            updated = pattern.sub(CODEX_AGENTS_BLOCK, original)
        else:
            updated = original.rstrip("\n") + "\n\n" + CODEX_AGENTS_BLOCK + "\n"

        if updated == original:
            return False, ""

        diff = _unified_diff(str(agents_path), original, updated)
        agents_path.write_text(updated, encoding="utf-8")
        return True, diff

    def verify_hooks(self, home: Path) -> tuple[bool, list[str]]:
        """Check that the Codex AGENTS.md managed block is present."""
        missing: list[str] = []
        codex_dir = home / ".codex"
        agents_path = _agents_path(codex_dir)

        if codex_dir.exists():
            content = agents_path.read_text(encoding="utf-8") if agents_path.exists() else ""
            if "<!-- mnemos:start -->" not in content or "<!-- mnemos:end -->" not in content:
                missing.append("codex AGENTS.md managed block")

        return (len(missing) == 0, missing)

    def uninstall(self, home: Path) -> list[str]:
        """Remove the Codex AGENTS.md managed block."""
        codex_dir = home / ".codex"
        changed, _ = self._remove_agents_md_block(codex_dir)
        label = str(_agents_path(codex_dir))
        return [f"[{'removed' if changed else 'unchanged'}] {label}"]

    def _remove_agents_md_block(self, codex_dir: Path) -> tuple[bool, str]:
        """Remove only the mnemos managed block from Codex AGENTS.md."""
        agents_path = _agents_path(codex_dir)
        if not agents_path.exists():
            return False, ""

        original = agents_path.read_text(encoding="utf-8")
        pattern = re.compile(
            r"\n?<!-- mnemos:start -->.*?<!-- mnemos:end -->\n?",
            re.DOTALL,
        )
        updated = pattern.sub("", original)
        updated = re.sub(r"\n{3,}", "\n\n", updated)

        if updated == original:
            return False, ""

        diff = _unified_diff(str(agents_path), original, updated)
        agents_path.write_text(updated, encoding="utf-8")
        return True, diff

    def subscribe_to_event_bus(self, bus: "EventBus") -> None:
        """Register no event handlers; Codex has no mnemos hook path here."""
        return None

