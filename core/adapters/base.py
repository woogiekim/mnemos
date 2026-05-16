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

### Capture interaction pattern
- Capture proactively — do NOT ask permission before calling `mnemos capture`
- The `mnemos capture` CLI output is the notification — do not add any separate message after capturing
- If the user says the capture is unnecessary, delete it: `mnemos delete <id>`
- Never prompt "Should I capture this?" — make the judgment and act"""


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
