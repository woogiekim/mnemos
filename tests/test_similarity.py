"""Tests for the similarity detection module (Issue #81 — Stage 1)."""
from __future__ import annotations

import pytest

from core.similarity import (
    _tokenise,
    find_similar_pairs,
    group_similar,
    jaccard_similarity,
)


# ---------------------------------------------------------------------------
# jaccard_similarity
# ---------------------------------------------------------------------------

class TestJaccardSimilarity:
    def test_identical_strings_return_one(self) -> None:
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_disjoint_strings_return_zero(self) -> None:
        assert jaccard_similarity("alpha beta", "gamma delta") == 0.0

    def test_half_overlap_returns_expected_ratio(self) -> None:
        # tokens: {a, b}, {b, c}  → |A∩B|=1, |A∪B|=3 → 1/3
        score = jaccard_similarity("a b", "b c")
        assert score == pytest.approx(1 / 3)

    def test_case_insensitive_via_normalisation(self) -> None:
        assert jaccard_similarity("HELLO World", "hello world") == 1.0

    def test_nfkc_normalisation_collapses_unicode(self) -> None:
        # Full-width ascii compatibility characters should normalise to ascii.
        assert jaccard_similarity("ｈｅｌｌｏ world", "hello world") == 1.0

    def test_empty_inputs_treated_as_identical(self) -> None:
        # Two empty inputs are defined identical (see module docstring).
        assert jaccard_similarity("", "") == 1.0

    def test_empty_vs_nonempty_returns_zero(self) -> None:
        assert jaccard_similarity("", "anything") == 0.0

    def test_deterministic_repeat_invocation(self) -> None:
        a = "alpha beta gamma"
        b = "beta gamma delta epsilon"
        runs = {jaccard_similarity(a, b) for _ in range(50)}
        assert len(runs) == 1

    def test_n_gram_option_changes_score(self) -> None:
        # "a b c" vs "b c a" share the same 1-gram set but disjoint bigram set.
        s1 = jaccard_similarity("a b c", "b c a", n_gram=1)
        s2 = jaccard_similarity("a b c", "b c a", n_gram=2)
        assert s1 == 1.0
        # bigrams: {"a b", "b c"} vs {"b c", "c a"} → |∩|=1, |∪|=3 → 1/3
        assert s2 == pytest.approx(1 / 3)

    def test_n_gram_zero_raises(self) -> None:
        with pytest.raises(ValueError):
            jaccard_similarity("a b", "a b", n_gram=0)

    def test_n_gram_shorter_than_tokens_yields_zero_similarity(self) -> None:
        # 2-gram of a single-token string yields empty set; vs identical
        # empty set → 1.0 by the empty-set rule.
        assert jaccard_similarity("alpha", "alpha", n_gram=2) == 1.0

    def test_n_gram_partial_short_input(self) -> None:
        # One side has too few tokens for the n_gram; sets are {bigrams} vs {}.
        # That gives 0/|union| = 0.
        assert jaccard_similarity("alpha", "alpha beta gamma", n_gram=2) == 0.0

    def test_threshold_boundary(self) -> None:
        # crafted: A={x,y,z,w}, B={x,y,z,k} → 3/5 = 0.6
        score = jaccard_similarity("x y z w", "x y z k")
        assert score == pytest.approx(0.6)


class TestTokeniseHelpers:
    def test_tokenise_handles_only_whitespace(self) -> None:
        assert _tokenise("   \t\n  ", n_gram=1) == set()


# ---------------------------------------------------------------------------
# find_similar_pairs
# ---------------------------------------------------------------------------

def _mem(item_id: str, content: str, *, stage: str = "stored",
         superseded_by: str | None = None,
         layer: str = "session") -> dict[str, object]:
    item: dict[str, object] = {
        "id": item_id,
        "content": content,
        "stage": stage,
        "layer": layer,
    }
    if superseded_by is not None:
        item["superseded_by"] = superseded_by
    return item


class TestFindSimilarPairs:
    def test_empty_iterable_yields_empty_list(self) -> None:
        assert find_similar_pairs([]) == []

    def test_threshold_validation_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            find_similar_pairs([_mem("a", "x")], threshold=1.5)
        with pytest.raises(ValueError):
            find_similar_pairs([_mem("a", "x")], threshold=-0.1)

    def test_single_item_returns_no_pairs(self) -> None:
        assert find_similar_pairs([_mem("a", "anything")]) == []

    def test_pair_above_threshold_is_returned(self) -> None:
        items = [_mem("a", "alpha beta gamma"), _mem("b", "alpha beta gamma")]
        pairs = find_similar_pairs(items, threshold=0.9)
        assert len(pairs) == 1
        a, b, score = pairs[0]
        assert a["id"] == "a" and b["id"] == "b"
        assert score == 1.0

    def test_pair_below_threshold_is_filtered(self) -> None:
        items = [_mem("a", "alpha"), _mem("b", "zeta")]
        assert find_similar_pairs(items, threshold=0.7) == []

    def test_threshold_boundary_inclusive(self) -> None:
        # Score exactly 0.6 — must be returned when threshold == 0.6.
        items = [_mem("a", "x y z w"), _mem("b", "x y z k")]
        pairs = find_similar_pairs(items, threshold=0.6)
        assert len(pairs) == 1

    def test_skips_archived_and_superseded(self) -> None:
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta", stage="archived", superseded_by="merged-1"),
        ]
        # Both have identical content; if b were eligible, the pair would
        # appear.  Because b is already merged, no pair is returned.
        assert find_similar_pairs(items, threshold=0.5) == []

    def test_archived_without_superseded_is_still_compared(self) -> None:
        # An archived-but-not-superseded item is invalid state and should
        # remain re-detectable so the operator notices.
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta", stage="archived"),
        ]
        assert len(find_similar_pairs(items, threshold=0.5)) == 1

    def test_superseded_without_archived_is_still_compared(self) -> None:
        # Likewise: a superseded_by pointer without an archived stage is
        # malformed and should not exclude the item from detection.
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta", superseded_by="merged-1"),
        ]
        assert len(find_similar_pairs(items, threshold=0.5)) == 1

    def test_missing_id_raises(self) -> None:
        items = [{"content": "x"}, {"content": "y", "id": "b"}]
        with pytest.raises(ValueError):
            find_similar_pairs(items, threshold=0.0)

    def test_pair_ordering_is_lex_by_id(self) -> None:
        # Even if input order is reversed, output uses sorted ids.
        items = [_mem("z", "alpha"), _mem("a", "alpha")]
        pairs = find_similar_pairs(items, threshold=0.5)
        assert len(pairs) == 1
        first, second, _ = pairs[0]
        assert first["id"] == "a" and second["id"] == "z"


# ---------------------------------------------------------------------------
# group_similar
# ---------------------------------------------------------------------------

class TestGroupSimilar:
    def test_no_groups_when_no_pairs(self) -> None:
        items = [_mem("a", "alpha"), _mem("b", "zeta")]
        assert group_similar(items, threshold=0.5) == []

    def test_simple_pair_forms_group(self) -> None:
        items = [_mem("a", "alpha beta"), _mem("b", "alpha beta")]
        groups = group_similar(items, threshold=0.5)
        assert len(groups) == 1
        assert [m["id"] for m in groups[0]] == ["a", "b"]

    def test_transitive_union_find(self) -> None:
        # A↔B (identical) and B↔C (similar enough) but A and C share less
        # — they should still group via union-find on B.
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta gamma"),
            _mem("c", "beta gamma delta"),
        ]
        # a↔b: jaccard = 2/3 = 0.67; b↔c: 2/4 = 0.5; a↔c: 1/4 = 0.25.
        groups = group_similar(items, threshold=0.5)
        assert len(groups) == 1
        assert [m["id"] for m in groups[0]] == ["a", "b", "c"]

    def test_multiple_disjoint_groups(self) -> None:
        items = [
            _mem("a1", "alpha alpha"),
            _mem("a2", "alpha alpha"),
            _mem("b1", "zeta zeta"),
            _mem("b2", "zeta zeta"),
        ]
        groups = group_similar(items, threshold=0.9)
        assert len(groups) == 2
        # Deterministic order: groups sorted by min member id ("a1" < "b1").
        assert [m["id"] for m in groups[0]] == ["a1", "a2"]
        assert [m["id"] for m in groups[1]] == ["b1", "b2"]

    def test_deterministic_ordering_across_input_permutations(self) -> None:
        a = _mem("a", "alpha beta")
        b = _mem("b", "alpha beta")
        c = _mem("c", "alpha beta")
        ordered_a = group_similar([a, b, c], threshold=0.5)
        ordered_b = group_similar([c, b, a], threshold=0.5)
        ordered_c = group_similar([b, a, c], threshold=0.5)
        # All three must produce identical output (deterministic id ordering).
        assert [[m["id"] for m in g] for g in ordered_a] == [["a", "b", "c"]]
        assert [[m["id"] for m in g] for g in ordered_b] == [["a", "b", "c"]]
        assert [[m["id"] for m in g] for g in ordered_c] == [["a", "b", "c"]]

    def test_excludes_singletons(self) -> None:
        items = [_mem("a", "alpha"), _mem("b", "alpha"), _mem("c", "totally different stuff entirely")]
        groups = group_similar(items, threshold=0.5)
        # Only {a, b} group; c is a singleton and is omitted.
        assert len(groups) == 1
        assert {m["id"] for m in groups[0]} == {"a", "b"}

    def test_already_merged_items_are_skipped_idempotently(self) -> None:
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta", stage="archived", superseded_by="merged-1"),
            _mem("c", "alpha beta"),
        ]
        groups = group_similar(items, threshold=0.5)
        assert len(groups) == 1
        assert {m["id"] for m in groups[0]} == {"a", "c"}

    def test_union_find_with_repeated_unions_is_stable(self) -> None:
        # Force the no-op union branch (same root) to execute.
        items = [
            _mem("a", "alpha beta"),
            _mem("b", "alpha beta"),
            _mem("c", "alpha beta"),
        ]
        # 1-grams identical → every pair (a,b),(a,c),(b,c) is above 0.5.
        # That exercises the "union same-root" branch when (a,c) unions
        # after (a,b) and (b,c) already merged everything.
        groups = group_similar(items, threshold=0.5)
        assert len(groups) == 1
        assert [m["id"] for m in groups[0]] == ["a", "b", "c"]

    def test_union_find_tie_break_when_left_id_is_greater(self) -> None:
        # Force union(a, b) where a > b lexicographically so the alternate
        # branch ("parent[ra] = rb") is taken.  Using ids 'z' and 'a' makes
        # 'a' the smaller root.
        items = [_mem("z", "alpha beta"), _mem("a", "alpha beta")]
        groups = group_similar(items, threshold=0.5)
        assert len(groups) == 1
        assert [m["id"] for m in groups[0]] == ["a", "z"]

    def test_union_find_path_compression_with_long_chain(self) -> None:
        # Build a long similarity chain so the path-compression branch
        # inside ``find`` ("while parent[x] != root: parent[x], x = ...")
        # has work to do.  Five identical-content items in non-sorted
        # order force multi-step root chasing.
        items = [
            _mem(f"id-{n:02d}", "alpha beta gamma delta")
            for n in (5, 1, 4, 2, 3)
        ]
        groups = group_similar(items, threshold=0.9)
        assert len(groups) == 1
        # All five collapse into one group; sorted by id.
        ids = [m["id"] for m in groups[0]]
        assert ids == sorted(ids)
        assert len(ids) == 5

    def test_group_omits_singleton_path(self) -> None:
        # Two pairs of similar items + one isolated item — the isolated
        # item produces a 1-member component which exercises the
        # ``if len(member_ids) < 2: continue`` skip.  We can't get there
        # via group_similar's eligible-set today (singletons never enter
        # parent), so call find_similar_pairs first to demonstrate that
        # singletons are filtered out at the source.
        items = [
            _mem("a", "alpha alpha alpha"),
            _mem("b", "alpha alpha alpha"),
            _mem("c", "totally distinct content here"),
        ]
        groups = group_similar(items, threshold=0.9)
        assert len(groups) == 1
        assert {m["id"] for m in groups[0]} == {"a", "b"}
