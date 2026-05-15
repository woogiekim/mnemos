"""Tests for SearchMiddleware and FTSIndex."""
import pytest
from pathlib import Path


@pytest.fixture
def fts_index(tmp_path):
    """Return an FTSIndex backed by a temp directory."""
    from mnemos.core.fts import FTSIndex
    db_path = str(tmp_path / "fts.db")
    return FTSIndex(db_path=db_path)


@pytest.fixture
def search_middleware(tmp_path):
    """Return a SearchMiddleware backed by a temp directory."""
    from mnemos.core.search import SearchMiddleware
    from mnemos.core.fts import FTSIndex
    db_path = str(tmp_path / "fts.db")
    fts = FTSIndex(db_path=db_path)
    return SearchMiddleware(repo_root=str(tmp_path), fts_index=fts)


class TestFTSIndex:
    def test_fts_index_and_search(self, fts_index):
        """Index an item then search for it by keyword."""
        fts_index.index_item(
            item_id="item-001",
            content="The quick brown fox jumps over the lazy dog",
            metadata={"layer": "global", "tags": []},
        )
        results = fts_index.search("fox")
        assert len(results) >= 1
        assert any(r["item_id"] == "item-001" for r in results)

    def test_search_returns_empty_on_no_match(self, fts_index):
        """Search with no indexed items returns empty list."""
        results = fts_index.search("nonexistent-term-xyz")
        assert results == []

    def test_fts_remove(self, fts_index):
        """Removed item should not appear in search results."""
        fts_index.index_item(
            item_id="item-remove",
            content="temporary content to be removed",
            metadata={},
        )
        results_before = fts_index.search("temporary")
        assert len(results_before) >= 1

        fts_index.remove("item-remove")
        results_after = fts_index.search("temporary")
        assert not any(r["item_id"] == "item-remove" for r in results_after)

    def test_fts_upsert_updates_content(self, fts_index):
        """Indexing same item_id again should update content."""
        fts_index.index_item(
            item_id="item-upsert",
            content="original content here",
            metadata={},
        )
        fts_index.index_item(
            item_id="item-upsert",
            content="completely new updated content",
            metadata={},
        )
        results = fts_index.search("updated")
        assert any(r["item_id"] == "item-upsert" for r in results)


class TestVectorFallback:
    def test_vector_fallback_when_backend_unavailable(self):
        """VectorBackend with backend=none returns empty list gracefully."""
        import os
        os.environ["MNEMOS_VECTOR_BACKEND"] = "none"
        from mnemos.core.vector import VectorBackend
        backend = VectorBackend()
        results = backend.search("some query")
        assert results == []

    def test_vector_fallback_on_invalid_backend(self):
        """VectorBackend with unknown backend returns empty list gracefully."""
        import os
        os.environ["MNEMOS_VECTOR_BACKEND"] = "invalid_backend"
        from mnemos.core.vector import VectorBackend
        backend = VectorBackend()
        results = backend.search("some query")
        assert results == []


class TestSearchMiddleware:
    def test_search_middleware_uses_fts_first(self, tmp_path):
        """SearchMiddleware returns FTS results when items are indexed."""
        from mnemos.core.fts import FTSIndex
        from mnemos.core.search import SearchMiddleware

        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mw-item-001",
            content="memory lifecycle management system",
            metadata={"layer": "global"},
        )

        middleware = SearchMiddleware(repo_root=str(tmp_path), fts_index=fts)
        results = middleware.search("lifecycle")
        assert len(results) >= 1
        assert any(r["item_id"] == "mw-item-001" for r in results)

    def test_search_middleware_deduplicates(self, tmp_path):
        """SearchMiddleware deduplicates results from multiple sources."""
        from mnemos.core.fts import FTSIndex
        from mnemos.core.search import SearchMiddleware

        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="dedup-item",
            content="deduplication test content",
            metadata={},
        )
        # Index same item again (simulate duplicate from two sources)
        fts.index_item(
            item_id="dedup-item",
            content="deduplication test content",
            metadata={},
        )

        middleware = SearchMiddleware(repo_root=str(tmp_path), fts_index=fts)
        results = middleware.search("deduplication")
        item_ids = [r["item_id"] for r in results]
        assert len(item_ids) == len(set(item_ids))

    def test_search_middleware_layer_filter(self, tmp_path):
        """SearchMiddleware filters results by layer when specified."""
        from mnemos.core.fts import FTSIndex
        from mnemos.core.search import SearchMiddleware

        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="global-item",
            content="global layer item content",
            metadata={"layer": "global"},
        )
        fts.index_item(
            item_id="project-item",
            content="project layer item content",
            metadata={"layer": "project"},
        )

        middleware = SearchMiddleware(repo_root=str(tmp_path), fts_index=fts)
        results = middleware.search("item content", layers=["global"])
        item_ids = [r["item_id"] for r in results]
        assert "global-item" in item_ids
        assert "project-item" not in item_ids
