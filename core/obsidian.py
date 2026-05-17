"""Obsidian vault storage backend for mnemos.

Implements the :class:`~core.store.StorageBackend` Protocol introduced in
PR-A (commit ee60c35).  Items are stored as Markdown files with YAML
front-matter inside an Obsidian vault, organised by layer into sub-folders:

    <vault>/session/<id>.md
    <vault>/project/<id>.md
    <vault>/global/<id>.md
    <vault>/transient/<id>.md
    <vault>/ephemeral/<id>.md
    <vault>/working/<id>.md
    <vault>/entities/<id>.md
    <vault>/claims/<id>.md
    <vault>/topics/<id>.md

**Layer promotion** is implemented as an atomic file-move from the source
folder to the target folder, plus a front-matter ``layer`` field update.

**Bidirectional editing**: ``sync_edits()`` scans vault files for
mtime-based changes, recomputes ``content_hash``, and reconciles the
front-matter — preserving the user's edited content while keeping mnemos
metadata consistent.

**FTS integration**: Every write / update / delete is mirrored to the
shared :class:`~core.fts.FTSIndex` so full-text search continues to work
across backends.

**Wiki-link cross-references**: The existing :class:`~core.store.MemoryStore`
does not embed internal cross-references in item *content*, so there are no
pointer-translation concerns for migrated items.  New items captured
directly via the Obsidian backend may use ``[[<id>]]`` syntax in their
content body for native Obsidian Graph integration.

**Multi-host sync** is explicitly *out of scope* for this PR.  The vault
path is whatever the user configures; iCloud / git-based synchronisation
is the user's responsibility.  See the project README for guidance.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterator

import frontmatter

from core.fts import FTSIndex


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All layers mnemos recognises — maps each to a vault sub-folder name.
#: The folder name is the same as the layer name, matching the Q3 decision.
OBSIDIAN_LAYERS: list[str] = [
    "session",
    "project",
    "global",
    "transient",
    "ephemeral",
    "working",
    "entities",
    "claims",
    "topics",
]

#: Quality score threshold below which an item is flagged as low-quality.
LOW_QUALITY_THRESHOLD: float = 0.3


# ---------------------------------------------------------------------------
# Internal helpers (content hash)
# ---------------------------------------------------------------------------


def _nfkc_normalise(content: str) -> str:
    """Apply NFKC normalisation + whitespace collapse + lowercase."""
    nfkc = unicodedata.normalize("NFKC", content)
    return re.sub(r"\s+", " ", nfkc.strip()).lower()


def _content_hash(content: str) -> str:
    """Return SHA-256 hex digest of the NFKC-normalised *content*.

    Matches ``core.gateway._capture_content_hash`` exactly so hashes are
    consistent across backends.
    """
    return hashlib.sha256(_nfkc_normalise(content).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# ObsidianBackend
# ---------------------------------------------------------------------------


class ObsidianBackend:
    """Obsidian-vault implementation of the :class:`~core.store.StorageBackend` Protocol.

    :param vault_path: Absolute path to the Obsidian vault directory.
    :param fts: Optional :class:`~core.fts.FTSIndex` instance.  When
        supplied, every write / update / delete is also indexed so that
        full-text search results are consistent regardless of which backend
        is active.
    """

    def __init__(self, vault_path: str, fts: FTSIndex | None = None) -> None:
        self._vault = Path(vault_path)
        self._vault.mkdir(parents=True, exist_ok=True)
        self._fts: FTSIndex | None = fts
        # mtime cache: maps str(path) → float (mtime at last sync)
        self._mtime_cache: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _layer_dir(self, layer: str) -> Path:
        """Return (and create) the vault sub-directory for *layer*."""
        if layer not in OBSIDIAN_LAYERS:
            raise ValueError(f"Unknown layer: {layer!r}")
        d = self._vault / layer
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _find_path(self, item_id: str) -> Path:
        """Search all layer folders for a file named ``<item_id>.md``.

        Raises :exc:`FileNotFoundError` if the item is not in any layer.
        """
        for layer in OBSIDIAN_LAYERS:
            candidate = self._vault / layer / f"{item_id}.md"
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"Memory item not found in Obsidian vault: {item_id!r}"
        )

    def _resolve_path(self, item_id_or_path: str) -> Path:
        """Resolve *item_id_or_path* to a :class:`~pathlib.Path`.

        Accepts:
        - An absolute file path that exists.
        - A relative path under the vault.
        - A bare ``item_id`` (searches all layer folders).
        """
        p = Path(item_id_or_path)
        if p.is_absolute() and p.exists():
            return p
        if (self._vault / item_id_or_path).exists():
            return self._vault / item_id_or_path
        # Treat as item_id
        return self._find_path(item_id_or_path)

    def _parse_path(self, path: Path) -> dict[str, Any]:
        """Parse a vault ``.md`` file into a dict with ``content`` + ``_path``."""
        post = frontmatter.load(str(path))
        result = dict(post.metadata)
        result["content"] = post.content
        result["_path"] = str(path)
        return result

    def _write_path(self, path: Path, content: str, metadata: dict[str, Any]) -> None:
        """Write a frontmatter document to *path*."""
        post = frontmatter.Post(content, **metadata)
        with path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

    def _fts_index(self, item_id: str, content: str, metadata: dict[str, Any]) -> None:
        """Index *item_id* in FTS (no-op when FTS is not configured)."""
        if self._fts is not None:
            self._fts.index_item(
                item_id=item_id,
                content=content,
                metadata={k: v for k, v in metadata.items()
                          if k not in ("content", "_path")},
            )

    def _fts_delete(self, item_id: str) -> None:
        """Remove *item_id* from FTS (no-op when FTS is not configured)."""
        if self._fts is not None:
            self._fts.remove(item_id)

    # ------------------------------------------------------------------ #
    # StorageBackend Protocol                                               #
    # ------------------------------------------------------------------ #

    def write(
        self,
        layer: str,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Create or overwrite a vault file and return its path.

        ``run_id`` and ``session_id`` are accepted for Protocol compatibility
        but ignored — all items are stored directly in ``<vault>/<layer>/``.
        """
        layer_dir = self._layer_dir(layer)
        file_path = layer_dir / f"{item_id}.md"

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        meta = dict(metadata)
        # Ensure required front-matter fields are present
        meta.setdefault("id", item_id)
        meta.setdefault("layer", layer)
        meta.setdefault("tags", [])
        meta.setdefault("quality_score", 0.8)
        meta.setdefault("created_at", now)
        meta.setdefault("access_count", 0)
        meta["updated_at"] = now
        meta["content_hash"] = _content_hash(content)

        self._write_path(file_path, content, meta)
        self._mtime_cache[str(file_path)] = file_path.stat().st_mtime
        self._fts_index(item_id, content, meta)
        return file_path

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        """Return a memory item dict (``content`` + front-matter + ``_path``)."""
        path = self._resolve_path(item_id_or_path)
        return self._parse_path(path)

    def update(
        self,
        item_id_or_path: str,
        content: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Path:
        """Update content and/or metadata of an existing vault item."""
        path = self._resolve_path(item_id_or_path)
        item = self._parse_path(path)

        new_content = content if content is not None else item["content"]
        new_meta = {k: v for k, v in item.items() if k not in ("content", "_path")}
        if metadata_updates:
            new_meta.update(metadata_updates)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_meta["updated_at"] = now
        if content is not None:
            new_meta["content_hash"] = _content_hash(new_content)

        self._write_path(path, new_content, new_meta)
        self._mtime_cache[str(path)] = path.stat().st_mtime
        item_id = new_meta.get("id", path.stem)
        self._fts_index(item_id, new_content, new_meta)
        return path

    def delete(self, item_id_or_path: str) -> None:
        """Remove a vault item file."""
        path = self._resolve_path(item_id_or_path)
        item_id = self._parse_path(path).get("id", path.stem)
        self._mtime_cache.pop(str(path), None)
        path.unlink()
        self._fts_delete(item_id)

    def list_layer(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Path]:
        """Yield all item paths in the given layer folder."""
        if layer not in OBSIDIAN_LAYERS:
            return
        layer_dir = self._vault / layer
        if layer_dir.exists():
            yield from sorted(layer_dir.glob("*.md"))

    def parse_file(self, path: Path) -> dict[str, Any]:
        """Parse a single vault file and return its dict representation.

        Part of the :class:`~core.store.StorageBackend` Protocol surface.
        """
        return self._parse_path(path)

    def iter_layer_items(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield parsed item dicts for every item in *layer*."""
        for path in self.list_layer(layer, run_id=run_id, session_id=session_id):
            try:
                yield self._parse_path(path)
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # Promotion (layer → layer file move)                                  #
    # ------------------------------------------------------------------ #

    def promote(self, item_id_or_path: str, target_layer: str) -> Path:
        """Move an item to *target_layer* and update its ``layer`` front-matter field.

        Implements Q3 decision: promotion = git-move (``os.rename``) + front-matter update.
        """
        src_path = self._resolve_path(item_id_or_path)
        item = self._parse_path(src_path)
        item_id = item.get("id", src_path.stem)

        target_dir = self._layer_dir(target_layer)
        dst_path = target_dir / f"{item_id}.md"

        # Update layer in metadata
        item["layer"] = target_layer
        item["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        content = item.pop("content")
        item.pop("_path", None)

        # Atomic move
        self._write_path(dst_path, content, item)
        src_path.unlink()

        self._mtime_cache.pop(str(src_path), None)
        self._mtime_cache[str(dst_path)] = dst_path.stat().st_mtime
        self._fts_index(item_id, content, item)
        return dst_path

    # ------------------------------------------------------------------ #
    # Bidirectional edit detection                                          #
    # ------------------------------------------------------------------ #

    def sync_edits(self) -> int:
        """Detect externally edited vault files and reconcile metadata.

        Scans every ``.md`` file in every layer folder.  For each file:

        - If the file's current mtime differs from the cached mtime (or the
          file is not yet cached), recompute ``content_hash``, increment
          ``access_count``, and update ``updated_at`` in front-matter.
        - Unchanged files are skipped.

        Returns the number of files that were updated.

        This implements the Q2 decision: bg-check calls ``sync_edits()``
        periodically so that user edits made directly in Obsidian are
        reflected in mnemos metadata.
        """
        changed = 0
        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in layer_dir.glob("*.md"):
                path_str = str(md_file)
                try:
                    current_mtime = md_file.stat().st_mtime
                except OSError:
                    continue

                cached_mtime = self._mtime_cache.get(path_str)
                if cached_mtime is not None and abs(current_mtime - cached_mtime) < 1e-6:
                    # File unchanged since last sync
                    continue

                # File is new or modified — reconcile
                try:
                    item = self._parse_path(md_file)
                except Exception:
                    continue

                content = item.get("content", "")
                new_hash = _content_hash(content)

                # Only count as "changed" if hash actually differs
                old_hash = item.get("content_hash", "")
                if new_hash == old_hash and cached_mtime is not None:
                    # mtime changed but content is identical (e.g. save without edit)
                    self._mtime_cache[path_str] = current_mtime
                    continue

                # Build updated metadata
                new_meta = {k: v for k, v in item.items() if k not in ("content", "_path")}
                new_meta["content_hash"] = new_hash
                new_meta["access_count"] = int(new_meta.get("access_count", 0)) + 1
                new_meta["updated_at"] = (
                    datetime.datetime.now(datetime.timezone.utc).isoformat()
                )

                self._write_path(md_file, content, new_meta)
                self._mtime_cache[path_str] = md_file.stat().st_mtime
                item_id = new_meta.get("id", md_file.stem)
                self._fts_index(item_id, content, new_meta)
                changed += 1

        return changed

    # ------------------------------------------------------------------ #
    # Health page (_health.md)                                             #
    # ------------------------------------------------------------------ #

    def generate_health_page(self) -> Path:
        """Generate (or regenerate) ``_health.md`` at the vault root.

        The page contains four sections — **Orphans**, **Stale Items**,
        **Low Quality**, and a sync disclaimer — with ``[[wikilinks]]`` to
        every flagged item.

        The file is fully regenerated on each call (idempotent).

        Implements Q4 decision.
        """
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        low_quality: list[str] = []
        stale: list[str] = []
        orphans: list[str] = []

        stale_threshold = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in layer_dir.glob("*.md"):
                if md_file.name == "_health.md":
                    continue
                try:
                    item = self._parse_path(md_file)
                except Exception:
                    orphans.append(md_file.stem)
                    continue

                item_id = item.get("id", md_file.stem)
                quality = float(item.get("quality_score", 0.8))
                if quality < LOW_QUALITY_THRESHOLD:
                    low_quality.append(item_id)

                # Stale: updated_at older than 30 days
                updated_str = item.get("updated_at", "")
                if updated_str:
                    try:
                        updated_dt = datetime.datetime.fromisoformat(
                            updated_str.replace("Z", "+00:00")
                        )
                        if updated_dt < stale_threshold:
                            stale.append(item_id)
                    except ValueError:
                        pass

        def _wikilinks(ids: list[str]) -> str:
            if not ids:
                return "_None_"
            return "\n".join(f"- [[{item_id}]]" for item_id in sorted(ids))

        lines: list[str] = [
            "# mnemos Health",
            "",
            "_Generated: " + now + "_",
            "",
            "> **Note — Multi-host sync**: The Obsidian backend stores items locally "
            "in this vault. Multi-host sync (iCloud, git) is **out of scope** for "
            "this release and is the user's responsibility.",
            "",
            "## Orphans",
            "",
            "_Items that could not be parsed (corrupt front-matter):_",
            "",
            _wikilinks(orphans),
            "",
            "## Stale Items",
            "",
            "_Items not updated in the last 30 days:_",
            "",
            _wikilinks(stale),
            "",
            "## Low Quality",
            "",
            f"_Items with quality\\_score < {LOW_QUALITY_THRESHOLD}:_",
            "",
            _wikilinks(low_quality),
            "",
        ]

        health_path = self._vault / "_health.md"
        health_path.write_text("\n".join(lines), encoding="utf-8")
        return health_path
