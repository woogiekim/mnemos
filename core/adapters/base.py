"""Abstract HostAdapter interface for mnemos host integrations."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


# ---------------------------------------------------------------------------
# Canonical behavioral ruleset shared across all adapters
# ---------------------------------------------------------------------------

MNEMOS_BEHAVIOR_BLOCK = """\
## Memory (mnemos)
When asked about past context, notes, or decisions, run:
`mnemos search <query>`
This searches your personal memory store managed by mnemos.

### Capturing knowledge
Capture liberally — when in doubt, capture. Use `mnemos capture "<insight>"` for:
- Stable project decisions
- Architecture constraints
- User preferences and working style
- Reusable workflows
- Important operational knowledge
- Persistent project context
- Any insight the user would want to recall in a future session

Do NOT capture:
- Temporary debugging steps or scratch work
- Transient one-off conversation with no lasting value

All captured insights begin in the session layer.
Promotion to project or global layer happens through mnemos promotion rules only.

### Capture interaction pattern
- Capture proactively — do NOT ask permission before calling `mnemos capture`
- Always use `mnemos capture --quiet` when capturing
- After capturing, notify in your own response text:
  - **session, project, global**: `✻ 🧠 <brief description> (<layer>)`
  - **ephemeral, working**: no notification
- If the user says the capture is unnecessary, delete it: `mnemos delete <id>`
- Never prompt "Should I capture this?" — make the judgment and act

### /compact — automatic session capture
When [mnemos] /compact detected appears in context:
- Immediately run `mnemos capture --quiet` for each significant insight, decision,
  or piece of context from the current session that has not already been captured
- Do this BEFORE the compact summary is generated
- Be thorough: this is the last chance to preserve session knowledge"""


class HostAdapter(ABC):
    """Abstract base class for mnemos host environment adapters.

    Each adapter owns the install/update/uninstall logic for a specific host
    (e.g. Claude Code, Cursor). The abstract interface ensures a uniform
    contract that the install, updater, and uninstaller orchestrators depend on.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable name of the host adapter (e.g. 'Claude Code')."""

    @abstractmethod
    def is_present(self, home: Path) -> bool:
        """Return True if this host is detected on the current machine.

        Args:
            home: The user's home directory (typically Path.home()).
        """

    @abstractmethod
    def install(self, home: Path) -> list[str]:
        """Perform installation steps for this host.

        Args:
            home: The user's home directory.

        Returns:
            A list of human-readable status messages describing what was done.
        """

    @abstractmethod
    def update(self, home: Path) -> list[str]:
        """Replace managed config blocks with their canonical versions.

        Args:
            home: The user's home directory.

        Returns:
            A list of human-readable status messages describing what was done.
        """

    @abstractmethod
    def uninstall(self, home: Path) -> list[str]:
        """Remove all managed config sections for this host.

        Args:
            home: The user's home directory.

        Returns:
            A list of human-readable status messages describing what was done.
        """

    @abstractmethod
    def verify_hooks(self, home: Path) -> tuple[bool, list[str]]:
        """Check whether all expected hooks/blocks are present for this host.

        Args:
            home: The user's home directory.

        Returns:
            A tuple of (all_present, missing_items) where:
              - all_present is True if every expected hook/block is registered
              - missing_items is a list of human-readable descriptions of what
                is absent (empty list when all_present is True)
        """
