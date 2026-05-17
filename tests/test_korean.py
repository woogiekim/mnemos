"""Tests for Korean query preprocessing (core/korean.py).

Covers:
- strip_particles: trailing particle removal
- expand_aliases: transliteration → English mapping
- preprocess_query: combined pipeline
- expand_query: multi-variant expansion for FTS fan-out
- Integration: FTSIndex and SearchMiddleware respect preprocessing
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Unit tests for core.korean
# ---------------------------------------------------------------------------


class TestStripParticles:
    """strip_particles removes exactly one trailing Korean particle."""

    def test_strips_single_char_particle(self):
        from core.korean import strip_particles
        assert strip_particles("니모스를") == "니모스"

    def test_strips_two_char_particle(self):
        from core.korean import strip_particles
        assert strip_particles("니모스의") == "니모스"
        assert strip_particles("검색에") == "검색"

    def test_strips_three_char_particle(self):
        from core.korean import strip_particles
        assert strip_particles("훅에서") == "훅"
        assert strip_particles("레이어으로") == "레이어"

    def test_strips_four_char_particle(self):
        from core.korean import strip_particles
        assert strip_particles("훅에서는") == "훅"

    def test_no_particle_unchanged(self):
        from core.korean import strip_particles
        assert strip_particles("검색") == "검색"
        assert strip_particles("에이전트크루") == "에이전트크루"

    def test_english_token_unchanged(self):
        from core.korean import strip_particles
        assert strip_particles("agent-crew") == "agent-crew"
        assert strip_particles("mnemos") == "mnemos"

    def test_whole_token_is_particle_returns_original(self):
        from core.korean import strip_particles
        # "을" is a particle; stripping it would leave empty string → return original
        assert strip_particles("을") == "을"

    def test_mixed_token_particle_only_suffix_stripped(self):
        from core.korean import strip_particles
        # Token has Korean body + particle suffix
        assert strip_particles("캡처를") == "캡처"

    def test_strips_longest_particle_first(self):
        from core.korean import strip_particles
        # "에서는" (4 chars) should be stripped as a whole, not just "는" (1 char)
        result = strip_particles("훅에서는")
        assert result == "훅"


class TestExpandAliases:
    """expand_aliases maps Korean transliterations to English equivalents."""

    def test_known_alias_replaced(self):
        from core.korean import expand_aliases
        assert expand_aliases("에이전트크루") == "agent-crew"
        assert expand_aliases("니모스") == "mnemos"
        assert expand_aliases("캡처") == "capture"
        assert expand_aliases("훅") == "hook"

    def test_unknown_token_unchanged(self):
        from core.korean import expand_aliases
        assert expand_aliases("unknown-term") == "unknown-term"
        assert expand_aliases("검색하고") == "검색하고"

    def test_english_token_unchanged(self):
        from core.korean import expand_aliases
        assert expand_aliases("agent-crew") == "agent-crew"

    def test_custom_alias_table(self):
        from core.korean import expand_aliases
        custom = {"커스텀": "custom-term"}
        assert expand_aliases("커스텀", aliases=custom) == "custom-term"
        # Default aliases no longer apply
        assert expand_aliases("에이전트크루", aliases=custom) == "에이전트크루"

    def test_empty_alias_table_returns_token(self):
        from core.korean import expand_aliases
        assert expand_aliases("에이전트크루", aliases={}) == "에이전트크루"

    def test_alias_match_is_case_insensitive(self):
        from core.korean import expand_aliases
        # Aliases are lowercased Korean; input that is already lower should match
        assert expand_aliases("에이전트크루") == "agent-crew"

    def test_particle_stripped_form_also_matches(self):
        from core.korean import expand_aliases, strip_particles
        # Strip particle first, then expand
        stripped = strip_particles("캡처율의")
        # "캡처율" maps to "capture" via alias
        result = expand_aliases(stripped)
        assert result == "capture"


class TestPreprocessQuery:
    """preprocess_query: combined particle stripping + alias expansion pipeline."""

    def test_bare_alias_expanded(self):
        from core.korean import preprocess_query
        assert preprocess_query("에이전트크루") == "agent-crew"
        assert preprocess_query("니모스") == "mnemos"

    def test_particle_attached_alias_expanded(self):
        from core.korean import preprocess_query
        # Particle is stripped, then alias is applied to stripped form
        assert preprocess_query("에이전트크루의") == "agent-crew"
        assert preprocess_query("니모스를") == "mnemos"

    def test_multi_token_query(self):
        from core.korean import preprocess_query
        result = preprocess_query("에이전트크루의 캡처율에서")
        assert result == "agent-crew capture"

    def test_mixed_korean_english_query(self):
        from core.korean import preprocess_query
        result = preprocess_query("니모스를 검색하고")
        # 니모스를 → mnemos; 검색하고 has no alias, strip 고 is not a particle → unchanged
        assert result.startswith("mnemos")

    def test_english_only_query_unchanged(self):
        from core.korean import preprocess_query
        assert preprocess_query("agent-crew search") == "agent-crew search"

    def test_empty_query_returned_as_is(self):
        from core.korean import preprocess_query
        assert preprocess_query("") == ""
        assert preprocess_query("   ") == "   "

    def test_zero_result_queries_from_issue(self):
        """Queries from the issue's zero-result audit should now produce non-empty output."""
        from core.korean import preprocess_query
        # Representative zero-result queries from the audit
        assert preprocess_query("에이전트크루") == "agent-crew"
        # 니모스의 → strip 의 → 니모스 → alias → mnemos
        assert preprocess_query("니모스의") == "mnemos"
        # 캡처율의 → strip 의 → 캡처율 → alias (캡처율) → capture
        assert preprocess_query("캡처율의") == "capture"
        # 훅에서 → strip 에서 → 훅 → alias → hook
        assert preprocess_query("훅에서") == "hook"


class TestExpandQuery:
    """expand_query returns 1-2 variants for FTS fan-out."""

    def test_english_query_single_variant(self):
        from core.korean import expand_query
        variants = expand_query("agent-crew")
        assert variants == ["agent-crew"]

    def test_korean_alias_returns_two_variants(self):
        from core.korean import expand_query
        variants = expand_query("에이전트크루")
        # First variant is the expanded English form
        assert "agent-crew" in variants
        # Second variant is the particle-stripped Korean form (no change here)
        assert "에이전트크루" in variants
        assert len(variants) == 2

    def test_particle_attached_returns_two_variants(self):
        from core.korean import expand_query
        variants = expand_query("니모스의")
        assert "mnemos" in variants
        assert "니모스" in variants

    def test_no_duplicates_in_variants(self):
        from core.korean import expand_query
        variants = expand_query("search")
        assert len(variants) == len(set(variants))


# ---------------------------------------------------------------------------
# Integration tests: FTSIndex respects Korean preprocessing
# ---------------------------------------------------------------------------


class TestFTSKoreanIntegration:
    """FTSIndex.search preprocesses Korean queries before hitting FTS5."""

    def test_particle_attached_query_finds_indexed_content(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-001",
            content="agent-crew is the memory system",
            metadata={"layer": "project"},
        )
        # Query with particle attachment — should still find "agent-crew"
        results = fts.search("에이전트크루의")
        assert len(results) >= 1
        assert any(r["item_id"] == "mem-001" for r in results)

    def test_alias_expansion_finds_english_indexed_content(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-002",
            content="mnemos capture lifecycle",
            metadata={"layer": "global"},
        )
        # "니모스를" → preprocess → "mnemos"
        results = fts.search("니모스를")
        assert len(results) >= 1
        assert any(r["item_id"] == "mem-002" for r in results)

    def test_english_query_still_works_unchanged(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-003",
            content="lifecycle management hooks",
            metadata={"layer": "session"},
        )
        results = fts.search("lifecycle")
        assert len(results) >= 1
        assert any(r["item_id"] == "mem-003" for r in results)

    def test_hook_alias_expansion(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-hook",
            content="UserPromptSubmit hook fires on every prompt",
            metadata={"layer": "project"},
        )
        # "훅에서" → strip "에서" → "훅" → alias → "hook"
        results = fts.search("훅에서")
        assert len(results) >= 1
        assert any(r["item_id"] == "mem-hook" for r in results)

    def test_capture_alias_with_particle(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-cap",
            content="capture stores memory items",
            metadata={"layer": "session"},
        )
        # "캡처율의" → strip "의" → "캡처율" → alias → "capture"
        results = fts.search("캡처율의")
        assert len(results) >= 1
        assert any(r["item_id"] == "mem-cap" for r in results)

    def test_zero_results_for_unindexed_term_still_zero(self, tmp_path):
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        fts.index_item(
            item_id="mem-unrelated",
            content="something completely different",
            metadata={},
        )
        results = fts.search("에이전트크루의")
        assert results == []

    def test_dedup_across_variants(self, tmp_path):
        """When both variants match, each item_id appears only once."""
        from core.fts import FTSIndex
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)
        # Index content that contains BOTH the English alias AND the Korean word
        fts.index_item(
            item_id="mem-both",
            content="mnemos 니모스 memory system",
            metadata={"layer": "project"},
        )
        results = fts.search("니모스를")
        item_ids = [r["item_id"] for r in results]
        assert item_ids.count("mem-both") == 1


# ---------------------------------------------------------------------------
# Integration tests: SearchMiddleware grep fallback respects Korean preprocessing
# ---------------------------------------------------------------------------


class TestSearchMiddlewareKoreanGrep:
    """SearchMiddleware grep fallback uses Korean preprocessing for text matching."""

    def test_grep_fallback_with_alias_expansion(self, tmp_path):
        from core.search import SearchMiddleware
        from core.fts import FTSIndex

        # Create an FTS DB that will NOT have any indexed content,
        # so the grep fallback fires.
        db_path = str(tmp_path / "fts.db")
        fts = FTSIndex(db_path=db_path)

        # Write a markdown file containing English content
        layer_dir = tmp_path / "wiki" / "project"
        layer_dir.mkdir(parents=True)
        md_file = layer_dir / "mem-grep.md"
        md_file.write_text("agent-crew is the best", encoding="utf-8")

        # Patch LAYER_STATIC_PATHS so grep finds the right directory
        import core.layers as layers_mod
        original_paths = dict(layers_mod.LAYER_STATIC_PATHS)
        layers_mod.LAYER_STATIC_PATHS["project"] = "wiki/project"

        try:
            middleware = SearchMiddleware(repo_root=str(tmp_path), fts_index=fts)
            # "에이전트크루의" → "agent-crew" — should match the file
            results = middleware.search("에이전트크루의")
            assert any(r.get("source") == "grep" for r in results), (
                "Expected grep fallback results; FTS should have returned nothing"
            )
        finally:
            layers_mod.LAYER_STATIC_PATHS.clear()
            layers_mod.LAYER_STATIC_PATHS.update(original_paths)
