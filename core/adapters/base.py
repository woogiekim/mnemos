"""Abstract HostAdapter interface for mnemos host integrations."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


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
