"""ClaudeMdScanner — discovers CLAUDE.md files for memory ingestion."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Each entry is (absolute_path, layer_name, source_scope)
ScanResult = tuple[Path, str, str]


class ClaudeMdScanner:
    """Discovers CLAUDE.md files from the global ~/.claude/ location and a project root.

    Attributes:
        project_root: Absolute path to the project directory.  A CLAUDE.md found
            directly under this directory is treated as a project-scoped file and
            routed to the ``"project"`` memory layer.
    """

    GLOBAL_CLAUDE_MD = Path.home() / ".claude" / "CLAUDE.md"

    def __init__(self, project_root: str | Path | None = None) -> None:
        self._project_root = Path(project_root).resolve() if project_root else Path.cwd().resolve()

    # ------------------------------------------------------------------ #
    # Public API                                                            #
    # ------------------------------------------------------------------ #

    def discover(self) -> list[ScanResult]:
        """Return a list of ``(path, layer, source_scope)`` tuples.

        Each tuple represents one CLAUDE.md file that should be ingested.
        Files that do not exist are silently skipped (a warning is logged).

        Returns:
            List of ``(Path, layer_str, scope_str)`` tuples ordered
            ``[global, project]`` when both exist.
        """
        candidates: list[tuple[Path, str, str]] = [
            (self.GLOBAL_CLAUDE_MD, "global", "global"),
            (self._project_root / "CLAUDE.md", "project", "project"),
        ]

        results: list[ScanResult] = []
        for path, layer, scope in candidates:
            if path.exists() and path.is_file():
                results.append((path.resolve(), layer, scope))
            else:
                logger.warning("CLAUDE.md not found, skipping: %s", path)

        return results
