"""Ingest Agent — reads raw/sources/ and captures each document into memory."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mnemos.gateway import MemoryGateway

logger = logging.getLogger(__name__)


class IngestAgent:
    """Reads files from raw/sources/ and calls memory-capture for each."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(
        self,
        source_dir: str,
        run_id: str = "default",
        layer: str = "ephemeral",
    ) -> list[str]:
        """
        Ingest all files from source_dir into memory.

        Args:
            source_dir: Path to the source directory (e.g., repo/raw/sources/).
            run_id: Run ID for ephemeral/working layer scoping.
            layer: Target memory layer (default: ephemeral).

        Returns:
            List of captured item IDs.
        """
        source_path = Path(source_dir)
        if not source_path.exists():
            logger.warning("Source directory does not exist: %s", source_dir)
            return []

        captured_ids: list[str] = []
        for file_path in source_path.iterdir():
            if not file_path.is_file():
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                item_id = self._gw.capture(
                    layer=layer,
                    content=content,
                    tags=["ingest", file_path.suffix.lstrip(".") or "txt"],
                    run_id=run_id,
                    extra_metadata={"source_file": str(file_path)},
                )
                captured_ids.append(item_id)
                logger.info("Ingested: %s → %s", file_path.name, item_id)
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", file_path, exc)

        return captured_ids
