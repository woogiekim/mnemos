"""Search Middleware — FTS → vector → grep fallback."""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.fts import FTSIndex
from core.korean import expand_query, preprocess_query
from core.layers import LAYER_STATIC_PATHS
from core.vector import VectorBackend

if TYPE_CHECKING:
    from core.store import StorageBackend


class SearchMiddleware:
    """
    Three-stage search pipeline:
    1. SQLite FTS5 (always available)
    2. Vector backend (optional, activated by env var)
    3. pathlib grep fallback
    """

    def __init__(
        self,
        repo_root: str,
        fts_index: FTSIndex | None = None,
        vector_backend: VectorBackend | None = None,
        store: "StorageBackend | None" = None,
    ) -> None:
        self._root = Path(repo_root)
        self._fts = fts_index or FTSIndex(db_path=str(self._root / ".agent" / "state" / "fts.db"))
        self._vector = vector_backend or VectorBackend()
        self._store = store

    def search(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Search across memory layers.

        Args:
            query: Search query string.
            layers: If provided, filter results to these layers only.
            limit: Maximum number of results to return.

        Returns:
            Ranked, deduplicated list of match dicts.
        """
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        # Stage 1: FTS5
        fts_results = self._fts.search(query, limit=limit)
        for r in fts_results:
            r_id = r["item_id"]
            if r_id not in seen_ids:
                if layers is None or r.get("metadata", {}).get("layer") in layers:
                    results.append(r)
                    seen_ids.add(r_id)

        # Stage 2: Vector search (if available)
        vector_results = self._vector.search(query, limit=limit)
        for r in vector_results:
            r_id = r.get("item_id", "")
            if r_id and r_id not in seen_ids:
                if layers is None or r.get("metadata", {}).get("layer") in layers:
                    results.append(r)
                    seen_ids.add(r_id)

        # Stage 3: pathlib grep fallback (if no results yet)
        if not results:
            grep_results = self._grep_fallback(query, layers=layers, limit=limit)
            for r in grep_results:
                r_id = r.get("item_id", "")
                if r_id and r_id not in seen_ids:
                    results.append(r)
                    seen_ids.add(r_id)

        return results[:limit]

    def _grep_fallback(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Python pathlib grep fallback — searches markdown files for query text.

        When a :class:`~core.store.StorageBackend` was supplied at construction,
        iteration goes through ``store.iter_layer_items`` so the storage
        abstraction is honoured.  Without a store the method falls back to
        direct filesystem globbing over the static layers (preserving the
        original behaviour for callers that do not yet pass a store).

        Korean queries are preprocessed (particle stripping + alias expansion)
        so that ``니모스를`` matches files containing ``mnemos``.
        """
        results: list[dict[str, Any]] = []
        # Build all query variants for Korean expansion
        query_variants = expand_query(query)
        query_lower_variants = [v.lower() for v in query_variants]

        seen_ids: set[str] = set()

        if self._store is not None:
            # Route through the StorageBackend Protocol.
            search_layers = list(LAYER_STATIC_PATHS.keys()) if layers is None else [
                l for l in layers if l in LAYER_STATIC_PATHS
            ]
            for layer in search_layers:
                for item in self._store.iter_layer_items(layer):
                    content = item.get("content", "")
                    # Also search YAML front-matter by serialising the full file text.
                    raw_path = item.get("_path", "")
                    try:
                        text = Path(raw_path).read_text(encoding="utf-8", errors="ignore") if raw_path else content
                    except OSError:
                        text = content
                    text_lower = text.lower()
                    if any(qv in text_lower for qv in query_lower_variants):
                        item_id = item.get("id") or (Path(raw_path).stem if raw_path else "")
                        if item_id in seen_ids:
                            continue
                        seen_ids.add(item_id)
                        results.append(
                            {
                                "item_id": item_id,
                                "content": text[:500],
                                "metadata": {},
                                "score": None,
                                "source": "grep",
                            }
                        )
                        if len(results) >= limit:
                            return results
        else:
            # Legacy path — direct filesystem globbing over static layers.
            search_dirs = self._get_search_dirs(layers)
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                for md_file in search_dir.glob("*.md"):
                    try:
                        text = md_file.read_text(encoding="utf-8", errors="ignore")
                        text_lower = text.lower()
                        if any(qv in text_lower for qv in query_lower_variants):
                            item_id = md_file.stem
                            if item_id in seen_ids:
                                continue
                            seen_ids.add(item_id)
                            results.append(
                                {
                                    "item_id": item_id,
                                    "content": text[:500],
                                    "metadata": {},
                                    "score": None,
                                    "source": "grep",
                                }
                            )
                            if len(results) >= limit:
                                return results
                    except OSError:
                        continue

        return results

    def _get_search_dirs(self, layers: list[str] | None) -> list[Path]:
        """Return directories to search based on layer filter.

        This method is used only on the legacy (no-store) path and is kept
        for backward compatibility.  New callers should pass a
        :class:`~core.store.StorageBackend` to the constructor instead.
        """
        all_dirs = {layer: self._root / path for layer, path in LAYER_STATIC_PATHS.items()}
        if layers is None:
            return list(all_dirs.values())
        return [all_dirs[l] for l in layers if l in all_dirs]
