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
        self._init_backend()

    def _init_backend(self) -> None:
        if self._backend == "qdrant":
            try:
                from qdrant_client import QdrantClient  # type: ignore
                url = os.environ.get("MNEMOS_QDRANT_URL", "http://localhost:6333")
                self._client = QdrantClient(url=url)
                self._available = True
            except (ImportError, Exception):
                self._available = False
        elif self._backend == "chroma":
            try:
                import chromadb  # type: ignore
                path = os.environ.get("MNEMOS_CHROMA_PATH", ".agent/state/chroma")
                self._client = chromadb.PersistentClient(path=path)
                self._available = True
            except (ImportError, Exception):
                self._available = False
        else:
            # backend is "none" or unknown — gracefully unavailable
            self._available = False

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """
        Search the vector backend.

        Returns empty list if backend is unavailable or returns no results.
        """
        if not self._available or self._client is None:
            return []

        try:
            if self._backend == "qdrant":
                return self._search_qdrant(query, limit)
            elif self._backend == "chroma":
                return self._search_chroma(query, limit)
        except Exception:
            # Network failure, schema mismatch, etc. — degrade gracefully
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
