"""Similar-memory detection — deterministic Jaccard + union-find grouping.

This module is the detection half of the compression pipeline (Issue #81).
It is intentionally lightweight and stdlib-only:

* :func:`jaccard_similarity` — set-based Jaccard over NFKC-normalised
  whitespace-tokenised content (optionally over n-grams of tokens).
* :func:`find_similar_pairs` — pairwise score sweep that returns only
  pairs above a threshold.  Items already merged (``stage == "archived"``
  AND ``superseded_by`` is set) are skipped so the operation is
  idempotent across runs.
* :func:`group_similar` — union-find collapse of pair output into groups
  of size >= 2.  Output is fully deterministic: groups are sorted by the
  minimum member id and each group's members are sorted by id.

The normalisation pattern mirrors ``core.gateway._nfkc_normalise`` by
construction — we deliberately replicate it locally rather than importing
the private helper.  This keeps the detection module self-contained and
prevents an accidental coupling to the gateway's on-write dedup contract.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Return NFKC-normalised, whitespace-collapsed, lowercased *text*.

    Mirrors :func:`core.gateway._nfkc_normalise` deliberately — see module
    docstring for the rationale.  Kept inline (not imported) so the
    detection module never depends on a private gateway symbol.
    """
    nfkc = unicodedata.normalize("NFKC", text)
    return _WHITESPACE_RE.sub(" ", nfkc.strip()).lower()


def _tokenise(content: str, n_gram: int) -> set[str]:
    """Split *content* into a set of n-gram tokens.

    ``n_gram == 1`` returns the bag of unique whitespace tokens.  Larger
    n-grams join consecutive tokens with a single space; sequences
    shorter than ``n_gram`` produce an empty set (no synthetic padding).
    """
    if n_gram < 1:
        raise ValueError(f"n_gram must be >= 1, got {n_gram}")
    tokens = _normalise(content).split(" ")
    # _normalise collapses runs of whitespace, but a wholly-empty input
    # still yields the single-element [""] list — strip it.
    tokens = [t for t in tokens if t]
    if not tokens:
        return set()
    if n_gram == 1:
        return set(tokens)
    if len(tokens) < n_gram:
        return set()
    return {" ".join(tokens[i:i + n_gram]) for i in range(len(tokens) - n_gram + 1)}


# ---------------------------------------------------------------------------
# Item shape helpers
# ---------------------------------------------------------------------------

def _item_id(item: Mapping[str, object]) -> str:
    """Return the canonical id of *item*.

    Memories carry their id under the ``id`` front-matter field.  We
    coerce to ``str`` so that pair-output ordering is stable even if a
    backend ever returns a non-string identifier.
    """
    raw = item.get("id")
    if raw is None:
        raise ValueError(f"item missing 'id' field: {item!r}")
    return str(raw)


def _is_already_merged(item: Mapping[str, object]) -> bool:
    """Return True when *item* should be skipped (already merged).

    A memory is considered already merged when it has been archived AND
    carries a ``superseded_by`` back-pointer.  Either alone is not
    sufficient — an item can be archived for unrelated reasons, and a
    ``superseded_by`` value without an ``archived`` stage is invalid
    state that should still be re-detected (so the operator notices).
    """
    if item.get("stage") != "archived":
        return False
    return bool(item.get("superseded_by"))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def jaccard_similarity(a: str, b: str, *, n_gram: int = 1) -> float:
    """Return the Jaccard similarity of *a* and *b* in ``[0.0, 1.0]``.

    Both inputs are NFKC-normalised, lowercased, and split on whitespace
    before set construction.  When ``n_gram > 1`` the tokens are joined
    into contiguous n-grams.  Two empty token sets are defined to be
    identical (score ``1.0``) — the more conservative ``0.0`` would
    make a no-content edge case appear similar-but-not-identical, which
    is the wrong signal for a merge-eligibility check.
    """
    set_a = _tokenise(a, n_gram)
    set_b = _tokenise(b, n_gram)
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        # Defensive: only possible when both sets are empty, already
        # handled above; included so the function is total.
        return 0.0  # pragma: no cover
    return len(set_a & set_b) / len(union)


def find_similar_pairs(
    items: Iterable[Mapping[str, object]],
    *,
    threshold: float = 0.7,
    n_gram: int = 1,
) -> list[tuple[Mapping[str, object], Mapping[str, object], float]]:
    """Return every pair of *items* whose Jaccard similarity is >= *threshold*.

    Each pair is emitted exactly once with the lexicographically-smaller
    id in the first position so callers can rely on the ordering.  Items
    that are already merged (see :func:`_is_already_merged`) are skipped
    entirely — they will not appear in any returned pair.

    The score field is included so a downstream ranking step can rank
    candidate groups by mean similarity without re-computing.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold}")

    # Materialise + filter once so we can deterministically order pair
    # output and iterate twice without exhausting an Iterable.
    eligible: list[Mapping[str, object]] = []
    for item in items:
        if _is_already_merged(item):
            continue
        eligible.append(item)

    # Sort eligibility list by id so output ordering is reproducible.
    eligible.sort(key=_item_id)

    pairs: list[tuple[Mapping[str, object], Mapping[str, object], float]] = []
    for i, a in enumerate(eligible):
        for b in eligible[i + 1:]:
            score = jaccard_similarity(
                str(a.get("content", "")),
                str(b.get("content", "")),
                n_gram=n_gram,
            )
            if score >= threshold:
                # _item_id returns lex order; eligible is already sorted
                # by id, so (a, b) is already in (lo, hi) order.
                pairs.append((a, b, score))
    return pairs


def group_similar(
    items: Sequence[Mapping[str, object]],
    *,
    threshold: float = 0.7,
    n_gram: int = 1,
) -> list[list[Mapping[str, object]]]:
    """Collapse pairwise similarity into groups via union-find.

    A group is a connected component over the pair graph induced by
    :func:`find_similar_pairs`.  Groups of size 1 (singletons) are
    omitted from the result — only groups eligible for merging (size
    >= 2) are returned.

    Output ordering is fully deterministic:
    * Each group's members are sorted by id (lexicographic).
    * Groups are sorted by their minimum member id.
    """
    pairs = find_similar_pairs(items, threshold=threshold, n_gram=n_gram)

    # Build parent map keyed by id (str) for union-find.  We only union
    # items that appear in at least one pair, so singletons drop out
    # naturally.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        # Iterative path-compression to keep recursion depth bounded.
        root = x
        while parent[root] != root:
            root = parent[root]
        # Compress.
        while parent[x] != root:  # pragma: no cover - tie-break union keeps depth <= 1
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        # Deterministic tie-break: smaller id wins as the canonical root.
        if ra < rb:
            parent[rb] = ra
        else:  # pragma: no cover - pairs are pre-sorted lex by find_similar_pairs
            parent[ra] = rb

    by_id: dict[str, Mapping[str, object]] = {}
    for a, b, _ in pairs:
        ida, idb = _item_id(a), _item_id(b)
        parent.setdefault(ida, ida)
        parent.setdefault(idb, idb)
        by_id[ida] = a
        by_id[idb] = b
        union(ida, idb)

    # Collect components.
    components: dict[str, list[str]] = {}
    for ident in parent:
        components.setdefault(find(ident), []).append(ident)

    # Drop singletons and sort deterministically.
    groups: list[list[Mapping[str, object]]] = []
    for member_ids in components.values():
        if len(member_ids) < 2:  # pragma: no cover - parent only contains paired ids
            continue
        member_ids.sort()
        groups.append([by_id[mid] for mid in member_ids])

    groups.sort(key=lambda g: _item_id(g[0]))
    return groups
