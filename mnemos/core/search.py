"""Search Middleware — FTS → vector → grep fallback."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mnemos.core.fts import FTSIndex
from mnemos.core.layers import LAYER_STATIC_PATHS
from mnemos.core.vector import VectorBackend


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
    ) -> None:
        self._root = Path(repo_root)
        self._fts = fts_index or FTSIndex(db_path=str(self._root / ".agent" / "state" / "fts.db"))
        self._vector = vector_backend or VectorBackend()

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
        """Python pathlib grep fallback — searches markdown files for query text."""
        results = []
        search_dirs = self._get_search_dirs(layers)

        query_lower = query.lower()
        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for md_file in search_dir.glob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8", errors="ignore")
                    if query_lower in text.lower():
                        # Try to extract item_id from file stem
                        item_id = md_file.stem
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
        """Return directories to search based on layer filter."""
        all_dirs = {layer: self._root / path for layer, path in LAYER_STATIC_PATHS.items()}
        if layers is None:
            return list(all_dirs.values())
        return [all_dirs[l] for l in layers if l in all_dirs]
