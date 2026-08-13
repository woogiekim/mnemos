"""Search Middleware — FTS → vector → grep fallback."""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.fts import FTSIndex
from core.korean import expand_query, preprocess_query
from core.layers import LAYER_STATIC_PATHS
from core.retrieval import rank_search_results
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
        self._vector = vector_backend or VectorBackend(
            repo_root=self._root,
            store=store,
        )
        self._store = store
        self._last_diagnostics: dict[str, Any] = _empty_search_diagnostics()

    def search(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
        allow_grep: bool = True,
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
        backend_traces: list[dict[str, Any]] = []
        degraded_reasons: list[str] = []
        partial_failure = False
        fallback_used = False

        # Stage 1: FTS5
        try:
            fts_results = self._fts.search(query, limit=limit)
            backend_traces.append(
                {
                    "name": "fts",
                    "status": "available",
                    "available": True,
                    "configured": True,
                    "result_count": len(fts_results),
                    "reason": None,
                }
            )
        except Exception as exc:
            fts_results = []
            partial_failure = True
            reason = _format_error(exc)
            degraded_reasons.append(f"fts: {reason}")
            backend_traces.append(
                {
                    "name": "fts",
                    "status": "error",
                    "available": False,
                    "configured": True,
                    "result_count": 0,
                    "reason": reason,
                }
            )

        for r in fts_results:
            r_id = r["item_id"]
            if r_id not in seen_ids:
                if layers is None or r.get("metadata", {}).get("layer") in layers:
                    results.append(r)
                    seen_ids.add(r_id)

        # Stage 2: Vector search (if available)
        try:
            vector_results = self._vector.search(query, limit=limit)
            vector_trace = _vector_trace(self._vector, len(vector_results))
        except Exception as exc:
            vector_results = []
            reason = _format_error(exc)
            vector_trace = {
                "name": "vector",
                "backend": getattr(self._vector, "backend_name", "unknown"),
                "status": "error",
                "available": False,
                "configured": True,
                "degraded": True,
                "result_count": 0,
                "reason": reason,
            }

        if vector_trace.get("degraded"):
            partial_failure = True
            reason = str(vector_trace.get("reason") or vector_trace.get("status") or "degraded")
            degraded_reasons.append(f"vector: {reason}")
        backend_traces.append(vector_trace)

        for r in vector_results:
            r_id = r.get("item_id", "")
            if r_id and r_id not in seen_ids:
                if layers is None or r.get("metadata", {}).get("layer") in layers:
                    results.append(r)
                    seen_ids.add(r_id)

        # Stage 3: pathlib grep fallback (if no results yet)
        if not results and allow_grep:
            fallback_used = True
            try:
                grep_results = self._grep_fallback(query, layers=layers, limit=limit)
                backend_traces.append(
                    {
                        "name": "grep",
                        "status": "used",
                        "available": True,
                        "configured": True,
                        "result_count": len(grep_results),
                        "reason": None,
                    }
                )
            except Exception as exc:
                grep_results = []
                partial_failure = True
                reason = _format_error(exc)
                degraded_reasons.append(f"grep: {reason}")
                backend_traces.append(
                    {
                        "name": "grep",
                        "status": "error",
                        "available": False,
                        "configured": True,
                        "result_count": 0,
                        "reason": reason,
                    }
                )
            for r in grep_results:
                r_id = r.get("item_id", "")
                if r_id and r_id not in seen_ids:
                    results.append(r)
                    seen_ids.add(r_id)
        elif not results:
            backend_traces.append(
                {
                    "name": "grep",
                    "status": "disabled",
                    "available": True,
                    "configured": True,
                    "result_count": 0,
                    "reason": "disabled by caller",
                }
            )
        else:
            backend_traces.append(
                {
                    "name": "grep",
                    "status": "skipped",
                    "available": True,
                    "configured": True,
                    "result_count": 0,
                    "reason": "not needed because earlier retrieval stages returned results",
                }
            )

        ranked = rank_search_results(query, results, limit=limit)
        self._last_diagnostics = {
            "status": "degraded" if partial_failure else "ok",
            "partial_failure": partial_failure,
            "fallback_used": fallback_used,
            "degraded_reasons": degraded_reasons,
            "backends": backend_traces,
            "result_count": len(ranked),
        }
        return ranked

    @property
    def last_diagnostics(self) -> dict[str, Any]:
        """Return diagnostics from the most recent search call."""
        return dict(self._last_diagnostics)

    def backend_health(self) -> dict[str, Any]:
        """Probe retrieval backend health without mutating memory."""
        backends: list[dict[str, Any]] = []
        degraded_reasons: list[str] = []

        try:
            probe_results = self._fts.search("__mnemos_backend_probe__", limit=1)
            backends.append(
                {
                    "name": "fts",
                    "status": "available",
                    "available": True,
                    "configured": True,
                    "result_count": len(probe_results),
                    "reason": None,
                }
            )
        except Exception as exc:
            reason = _format_error(exc)
            degraded_reasons.append(f"fts: {reason}")
            backends.append(
                {
                    "name": "fts",
                    "status": "error",
                    "available": False,
                    "configured": True,
                    "result_count": 0,
                    "reason": reason,
                }
            )

        vector_trace = _vector_trace(self._vector, result_count=0)
        if vector_trace.get("degraded"):
            reason = str(vector_trace.get("reason") or vector_trace.get("status") or "degraded")
            degraded_reasons.append(f"vector: {reason}")
        backends.append(vector_trace)

        fallback = _grep_health(self._root, self._store)
        if fallback["status"] == "unavailable":
            degraded_reasons.append(f"grep: {fallback['reason']}")
        backends.append(fallback)

        fts_available = bool(backends[0].get("available"))
        fallback_available = bool(fallback.get("available"))
        if not fts_available and not fallback_available:
            status = "failed"
        elif degraded_reasons:
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "partial_failure": status != "ok",
            "retrieval_contract": "fts-primary-vector-optional-grep-fallback",
            "degraded_reasons": degraded_reasons,
            "backends": backends,
        }

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


def _empty_search_diagnostics() -> dict[str, Any]:
    """Return the default search diagnostics payload."""
    return {
        "status": "unknown",
        "partial_failure": False,
        "fallback_used": False,
        "degraded_reasons": [],
        "backends": [],
        "result_count": 0,
    }


def _vector_trace(vector_backend: Any, result_count: int) -> dict[str, Any]:
    """Return a normalized trace for a vector backend instance."""
    if hasattr(vector_backend, "diagnostics"):
        diagnostics = dict(vector_backend.diagnostics())
    else:
        available = bool(getattr(vector_backend, "is_available", False))
        diagnostics = {
            "name": "vector",
            "backend": getattr(vector_backend, "backend_name", "unknown"),
            "configured": available,
            "available": available,
            "status": "available" if available else "disabled",
            "degraded": False,
            "reason": None if available else "vector backend is not configured",
        }

    diagnostics["name"] = "vector"
    diagnostics["result_count"] = result_count
    return diagnostics


def _grep_health(root: Path, store: Any | None) -> dict[str, Any]:
    """Return availability metadata for the grep fallback path."""
    if store is not None and hasattr(store, "iter_layer_items"):
        return {
            "name": "grep",
            "status": "available",
            "available": True,
            "configured": True,
            "result_count": 0,
            "reason": "storage backend iteration is available",
        }

    existing_dirs = [
        str(root / path)
        for path in LAYER_STATIC_PATHS.values()
        if (root / path).exists()
    ]
    if existing_dirs:
        return {
            "name": "grep",
            "status": "available",
            "available": True,
            "configured": True,
            "result_count": 0,
            "reason": f"{len(existing_dirs)} searchable layer directories exist",
        }

    return {
        "name": "grep",
        "status": "unavailable",
        "available": False,
        "configured": True,
        "result_count": 0,
        "reason": "no searchable layer directories were found",
    }


def _format_error(exc: Exception) -> str:
    """Return a compact, JSON-safe exception summary."""
    message = str(exc)
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__
