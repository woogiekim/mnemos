"""Vector Search Stub — graceful fallback when vector backend is unavailable."""
from __future__ import annotations

import os
from typing import Any


class VectorBackend:
    """
    Optional vector search backend.

    Activated by setting MNEMOS_VECTOR_BACKEND=qdrant|chroma.
    Falls back to empty results if the backend is unavailable or not configured.
    """

    def __init__(self) -> None:
        self._backend = os.environ.get("MNEMOS_VECTOR_BACKEND", "none").lower()
        self._client = None
        self._available = False
        self._last_error: str | None = None
        self._last_search_error: str | None = None
        self._init_backend()

    def _init_backend(self) -> None:
        self._last_error = None
        if self._backend == "qdrant":
            try:
                from qdrant_client import QdrantClient  # type: ignore
                url = os.environ.get("MNEMOS_QDRANT_URL", "http://localhost:6333")
                self._client = QdrantClient(url=url)
                self._available = True
            except Exception as exc:
                self._available = False
                self._last_error = _format_error(exc)
        elif self._backend == "chroma":
            try:
                import chromadb  # type: ignore
                path = os.environ.get("MNEMOS_CHROMA_PATH", ".agent/state/chroma")
                self._client = chromadb.PersistentClient(path=path)
                self._available = True
            except Exception as exc:
                self._available = False
                self._last_error = _format_error(exc)
        elif self._backend == "none":
            self._available = False
        else:
            self._available = False
            self._last_error = f"unsupported vector backend: {self._backend}"

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search the vector backend.

        Returns empty list if backend is unavailable or returns no results.
        """
        self._last_search_error = None
        if not self._available or self._client is None:
            return []

        try:
            if self._backend == "qdrant":
                return self._search_qdrant(query, limit)
            elif self._backend == "chroma":
                return self._search_chroma(query, limit)
        except Exception as exc:
            # Network failure, schema mismatch, etc. — degrade gracefully
            self._last_search_error = _format_error(exc)
            return []

        return []

    def _search_qdrant(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Qdrant vector search (requires qdrant-client and a running server)."""
        # In a production implementation this would embed the query and search.
        # Stub: return empty until embeddings are configured.
        return []

    def _search_chroma(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Chroma vector search (requires chromadb)."""
        # In a production implementation this would query the default collection.
        # Stub: return empty until embeddings are configured.
        return []

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def backend_name(self) -> str:
        """Return the configured vector backend name."""
        return self._backend

    @property
    def is_configured(self) -> bool:
        """Return True when a non-disabled vector backend was requested."""
        return self._backend != "none"

    def diagnostics(self) -> dict[str, Any]:
        """Return operational health metadata for the vector backend."""
        if self._last_search_error:
            status = "degraded"
            reason = self._last_search_error
        elif self._available:
            status = "available"
            reason = None
        elif self._backend == "none":
            status = "disabled"
            reason = "vector backend is not configured"
        elif self._backend not in {"qdrant", "chroma"}:
            status = "unsupported"
            reason = self._last_error or f"unsupported vector backend: {self._backend}"
        else:
            status = "unavailable"
            reason = self._last_error or "vector backend is unavailable"

        return {
            "name": "vector",
            "backend": self._backend,
            "configured": self.is_configured,
            "available": self._available,
            "status": status,
            "degraded": self.is_configured and status != "available",
            "reason": reason,
        }


def _format_error(exc: Exception) -> str:
    """Return a compact, JSON-safe exception summary."""
    message = str(exc)
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__
