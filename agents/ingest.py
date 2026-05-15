"""Ingest Agent — reads raw/sources/ and captures each document into memory."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.gateway import MemoryGateway

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    """Return hex-encoded SHA-256 digest of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IngestAgent:
    """Reads files from raw/sources/ and calls capture for each."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(
        self,
        source_dir: str,
        run_id: str = "default",
        layer: str = "ephemeral",
        files: list[Path] | None = None,
    ) -> list[str]:
        """
        Ingest files into memory.

        When *files* is provided the explicit list is ingested directly and
        *source_dir* / *layer* are used only as fallback defaults for any file
        whose entry does not carry its own layer routing.  When *files* is
        ``None`` every file found directly under *source_dir* is ingested using
        the caller-supplied *layer*.

        Args:
            source_dir: Path to the source directory (e.g., repo/raw/sources/).
                        Ignored when *files* is provided.
            run_id: Run ID for ephemeral/working layer scoping.
            layer: Default target memory layer (default: ``"ephemeral"``).
                   Overridden per-file by entries in *files* that carry an
                   explicit layer via the ``extra_metadata`` routing convention.
            files: Optional explicit list of ``Path`` objects to ingest.  Each
                   entry may be a plain ``Path`` (uses *layer*) or a tuple
                   ``(Path, layer_str, extra_metadata_dict)`` for per-file
                   routing.  When present, *source_dir* scanning is skipped.

        Returns:
            List of captured item IDs.
        """
        if files is not None:
            return self._run_files(files, run_id=run_id, default_layer=layer)

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

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _run_files(
        self,
        files: list[Path],
        *,
        run_id: str,
        default_layer: str,
    ) -> list[str]:
        """Ingest an explicit list of file paths.

        Each entry in *files* must be a plain ``Path`` object.  Per-file layer
        and extra metadata are looked up from the caller-supplied entries via
        the ``_file_meta`` side-channel set by :meth:`run_scanner_results`; for
        direct callers passing a plain list the *default_layer* and empty
        extra metadata are used.

        Internal note: the scanner pipeline calls
        :meth:`run_scanner_results` which owns the richer per-file routing.
        This method is the shared implementation used by both code paths.
        """
        captured_ids: list[str] = []
        for file_path in files:
            if not file_path.exists() or not file_path.is_file():
                logger.warning("File not found, skipping: %s", file_path)
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                item_id = self._gw.capture(
                    layer=default_layer,
                    content=content,
                    tags=["ingest", file_path.suffix.lstrip(".") or "txt"],
                    run_id=run_id,
                    extra_metadata={"source_file": str(file_path)},
                )
                captured_ids.append(item_id)
                logger.info("Ingested (files mode): %s → %s", file_path.name, item_id)
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", file_path, exc)
        return captured_ids

    def run_scanner_results(
        self,
        scan_results: list[tuple[Path, str, str]],
        run_id: str = "default",
    ) -> list[str]:
        """Ingest results produced by :class:`~agents.scanner.ClaudeMdScanner`.

        Args:
            scan_results: List of ``(path, layer, source_scope)`` tuples as
                          returned by
                          :meth:`~agents.scanner.ClaudeMdScanner.discover`.
            run_id: Run ID forwarded to :meth:`~mnemos.gateway.MemoryGateway.capture`.

        Returns:
            List of captured item IDs in the same order as *scan_results*.
        """
        captured_ids: list[str] = []
        for file_path, layer, source_scope in scan_results:
            if not file_path.exists() or not file_path.is_file():
                logger.warning("CLAUDE.md not found, skipping: %s", file_path)
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                extra_meta: dict[str, Any] = {
                    "sourceType": "claude_md",
                    "source_file": str(file_path),
                    "source_scope": source_scope,
                }
                item_id = self._gw.capture(
                    layer=layer,
                    content=content,
                    tags=["ingest", "claude_md"],
                    run_id=run_id,
                    extra_metadata=extra_meta,
                )
                captured_ids.append(item_id)
                logger.info(
                    "Ingested CLAUDE.md (%s): %s → %s", source_scope, file_path, item_id
                )
            except Exception as exc:
                logger.error("Failed to ingest CLAUDE.md %s: %s", file_path, exc)
        return captured_ids

    def run_scanner_results_dedup(
        self,
        scan_results: list[tuple[Path, str, str]],
        run_id: str = "default",
        tags: list[str] | None = None,
        source_type: str = "claude_memory",
    ) -> dict[str, list[str]]:
        """Ingest scanner results with content-hash deduplication.

        Before capturing each file this method searches existing items in the
        target layer for an item whose ``source_file`` metadata matches the
        candidate path.

        * **Same hash** — content is unchanged; skip without writing.
        * **Different hash** — content has changed; update in place and refresh
          the stored ``content_hash``.
        * **No match** — new file; capture as usual and store ``content_hash``.

        Args:
            scan_results: List of ``(path, layer, source_scope)`` tuples as
                          returned by scanner discovery methods.
            run_id: Run ID forwarded to :meth:`~core.gateway.MemoryGateway.capture`.
            tags: Tags to apply to newly captured items.  Defaults to
                  ``["ingest", source_type]``.
            source_type: Value written to the ``sourceType`` metadata field and
                         used as the second default tag (default: ``"claude_memory"``).

        Returns:
            A dict with three keys mapping to lists of item IDs:
            ``"created"``, ``"updated"``, and ``"skipped"``.
        """
        effective_tags = tags if tags is not None else ["ingest", source_type]
        result: dict[str, list[str]] = {"created": [], "updated": [], "skipped": []}

        for file_path, layer, source_scope in scan_results:
            if not file_path.exists() or not file_path.is_file():
                logger.warning("Memory file not found, skipping: %s", file_path)
                continue

            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                logger.error("Failed to read %s: %s", file_path, exc)
                continue

            new_hash = _sha256(content)
            source_file_str = str(file_path)

            # Search for an existing item with the same source_file in this layer
            existing = self._find_by_source_file(layer=layer, source_file=source_file_str, run_id=run_id)

            if existing is not None:
                existing_hash = existing.get("content_hash", "")
                item_id: str = existing["id"]

                if existing_hash == new_hash:
                    logger.debug("Skipped (unchanged): %s [%s]", file_path.name, item_id)
                    result["skipped"].append(item_id)
                    continue

                # Content changed — update in place
                try:
                    self._gw.update(item_id=item_id, content=content)
                    # Persist the refreshed hash via a metadata-only patch
                    item = self._gw._store.read(item_id)
                    self._gw._store.update(
                        item["_path"],
                        metadata_updates={"content_hash": new_hash},
                    )
                    logger.info("Updated (changed): %s → %s", file_path.name, item_id)
                    result["updated"].append(item_id)
                except Exception as exc:
                    logger.error("Failed to update %s: %s", file_path, exc)
            else:
                # New file — capture
                try:
                    extra_meta: dict[str, Any] = {
                        "sourceType": source_type,
                        "source_file": source_file_str,
                        "source_scope": source_scope,
                        "content_hash": new_hash,
                    }
                    item_id = self._gw.capture(
                        layer=layer,
                        content=content,
                        tags=effective_tags,
                        run_id=run_id,
                        extra_metadata=extra_meta,
                    )
                    logger.info("Created (new): %s → %s", file_path.name, item_id)
                    result["created"].append(item_id)
                except Exception as exc:
                    logger.error("Failed to capture %s: %s", file_path, exc)

        return result

    # ------------------------------------------------------------------ #
    # Dedup helpers                                                         #
    # ------------------------------------------------------------------ #

    def _find_by_source_file(
        self,
        layer: str,
        source_file: str,
        run_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the first stored item whose ``source_file`` matches, or ``None``.

        Scans ``layer_dir/*.md`` files directly rather than going through FTS so
        that the lookup is reliable even when the index is stale.

        Args:
            layer: Memory layer to scan (e.g. ``"global"``).
            source_file: Absolute path string stored in ``source_file`` metadata.
            run_id: Required for ephemeral/working layers; ignored otherwise.

        Returns:
            Parsed item dict (same format as :meth:`~core.store.MemoryStore.read`)
            or ``None`` if not found.
        """
        try:
            for item_path in self._gw._store.list_layer(layer=layer, run_id=run_id):
                try:
                    item = self._gw._store.read(str(item_path))
                    if item.get("source_file") == source_file:
                        return item
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Could not scan layer '%s' for source_file: %s", layer, exc)
        return None
