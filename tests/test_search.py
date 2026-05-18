"""Tests for SearchMiddleware and FTSIndex."""
import pytest
from pathlib import Path


@pytest.fixture
def fts_index(tmp_path):
    """Return an FTSIndex backed by a temp directory."""
    from core.fts import FTSIndex
    db_path = str(tmp_path / "fts.db")
    return FTSIndex(db_path=db_path)


@pytest.fixture
def search_middleware(tmp_path):
    """Return a SearchMiddleware backed by a temp directory."""
    from core.search import SearchMiddleware
    from core.fts import FTSIndex
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
        from core.vector import VectorBackend
        backend = VectorBackend()
        results = backend.search("some query")
        assert results == []

    def test_vector_fallback_on_invalid_backend(self):
        """VectorBackend with unknown backend returns empty list gracefully."""
        import os
        os.environ["MNEMOS_VECTOR_BACKEND"] = "invalid_backend"
        from core.vector import VectorBackend
        backend = VectorBackend()
        results = backend.search("some query")
        assert results == []


class TestFTSFrontmatterStripping:
    """Tests for YAML front-matter stripping in the FTS index (issue #26).

    Memory files may be ingested as raw text including a leading ``--- ... ---``
    front-matter block.  That block must not appear in FTS snippets — it adds
    noise (metadata key/value lines) that obscures the actual memory content.
    """

    def test_frontmatter_not_in_indexed_content(self, fts_index):
        """Content indexed with YAML front-matter should not surface the front-matter in results."""
        raw = (
            "---\n"
            "id: abc-123\n"
            "layer: global\n"
            "tags: [architecture]\n"
            "---\n"
            "Architecture decision: use hexagonal design."
        )
        fts_index.index_item(item_id="fm-item", content=raw, metadata={})

        # Search for the body content — must still match
        results = fts_index.search("hexagonal")
        assert any(r["item_id"] == "fm-item" for r in results)

        # The stored content must not contain the front-matter lines
        hit = next(r for r in results if r["item_id"] == "fm-item")
        assert "id: abc-123" not in hit["content"]
        assert "layer: global" not in hit["content"]
        assert "---" not in hit["content"]

    def test_frontmatter_fields_not_searchable_after_indexing(self, fts_index):
        """FTS search on a front-matter key should not return the item.

        Once the front-matter is stripped, terms that appear *only* in the
        front-matter block are no longer in the FTS index for that item.
        The body content remains searchable.
        """
        raw = (
            "---\n"
            "unique_fm_key: uniquefmvalue9999\n"
            "---\n"
            "Body text that does not mention the frontmatter value."
        )
        fts_index.index_item(item_id="fm-noisy", content=raw, metadata={})

        # 'uniquefmvalue9999' is only in the front-matter — should not match
        results_fm = fts_index.search("uniquefmvalue9999")
        assert not any(r["item_id"] == "fm-noisy" for r in results_fm)

        # But the body is still findable
        results_body = fts_index.search("Body")
        assert any(r["item_id"] == "fm-noisy" for r in results_body)

    def test_content_without_frontmatter_unchanged(self, fts_index):
        """Plain content (no front-matter) is indexed and searched without modification."""
        plain = "No front-matter here — just plain text content."
        fts_index.index_item(item_id="plain-item", content=plain, metadata={})

        results = fts_index.search("plain")
        assert any(r["item_id"] == "plain-item" for r in results)
        hit = next(r for r in results if r["item_id"] == "plain-item")
        assert "No front-matter" in hit["content"]

    def test_strip_frontmatter_helper_removes_block(self):
        """_strip_frontmatter() removes the leading YAML block and returns body."""
        from core.fts import _strip_frontmatter

        raw = "---\nkey: value\n---\nBody text here."
        assert _strip_frontmatter(raw) == "Body text here."

    def test_strip_frontmatter_helper_noop_on_plain_content(self):
        """_strip_frontmatter() returns the original string when no front-matter present."""
        from core.fts import _strip_frontmatter

        plain = "Just a plain string."
        assert _strip_frontmatter(plain) == plain

    def test_strip_frontmatter_helper_multiline_block(self):
        """_strip_frontmatter() handles multi-field YAML blocks."""
        from core.fts import _strip_frontmatter

        raw = "---\nid: x\nlayer: session\ntags:\n  - foo\n---\nThe actual content."
        result = _strip_frontmatter(raw)
        assert result == "The actual content."
        assert "---" not in result
        assert "layer:" not in result


class TestFTSCompoundTerms:
    """Tests for compound technical term handling (issue #30).

    Covers queries containing colons (e.g. ``crew:run``) and hyphens
    (e.g. ``agent-crew``).  The FTS index must normalise such terms to
    space-separated tokens so that documents are found even when the
    full compound form is absent from the content.
    """

    def test_search_colon_term_matches_exact_compound(self, fts_index):
        """Searching 'crew:run' finds a document that contains 'crew:run'."""
        fts_index.index_item(
            item_id="colon-exact",
            content="Use crew:run to launch the agent pipeline.",
            metadata={},
        )
        results = fts_index.search("crew:run")
        assert any(r["item_id"] == "colon-exact" for r in results)

    def test_search_colon_term_matches_space_separated_content(self, fts_index):
        """Searching 'crew:run' also finds docs where 'crew' and 'run' appear separately."""
        fts_index.index_item(
            item_id="colon-space",
            content="The crew system is used to run agent tasks across the pipeline.",
            metadata={},
        )
        results = fts_index.search("crew:run")
        assert any(r["item_id"] == "colon-space" for r in results)

    def test_search_hyphen_term_matches_exact_compound(self, fts_index):
        """Searching 'agent-crew' finds a document that contains 'agent-crew'."""
        fts_index.index_item(
            item_id="hyphen-exact",
            content="The agent-crew system manages parallel task execution.",
            metadata={},
        )
        results = fts_index.search("agent-crew")
        assert any(r["item_id"] == "hyphen-exact" for r in results)

    def test_search_hyphen_term_matches_space_separated_content(self, fts_index):
        """Searching 'agent-crew' also finds docs where 'agent' and 'crew' appear separately."""
        fts_index.index_item(
            item_id="hyphen-space",
            content="Each agent in the crew is responsible for a specific pipeline stage.",
            metadata={},
        )
        results = fts_index.search("agent-crew")
        assert any(r["item_id"] == "hyphen-space" for r in results)

    def test_search_compound_no_operationalerror(self, fts_index):
        """Searching compound terms must not raise OperationalError."""
        # Both crew:run and agent-crew would raise OperationalError without sanitisation.
        fts_index.index_item(
            item_id="compound-safe",
            content="Documentation for the crew pipeline management system.",
            metadata={},
        )
        # These calls must complete without exception.
        fts_index.search("crew:run")
        fts_index.search("agent-crew")

    def test_normalise_compound_terms_helper(self):
        """_normalise_compound_terms replaces colons and hyphens with spaces."""
        from core.fts import _normalise_compound_terms

        assert _normalise_compound_terms("crew:run") == "crew run"
        assert _normalise_compound_terms("agent-crew") == "agent crew"
        assert _normalise_compound_terms("foo:bar-baz") == "foo bar baz"
        assert _normalise_compound_terms("no_special_chars") == "no_special_chars"
        assert _normalise_compound_terms("plain words only") == "plain words only"


class TestSearchMiddleware:
    def test_search_middleware_uses_fts_first(self, tmp_path):
        """SearchMiddleware returns FTS results when items are indexed."""
        from core.fts import FTSIndex
        from core.search import SearchMiddleware

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
        from core.fts import FTSIndex
        from core.search import SearchMiddleware

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
        from core.fts import FTSIndex
        from core.search import SearchMiddleware

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
