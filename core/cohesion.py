"""Domain / policy cohesion abstraction over stored memories (issue #67).

This module is a **read-only derivation layer** over the dict-shaped memory
items mnemos already stores. It introduces three higher-level views:

1. **Domains** — cohesive clusters of related memories, derived primarily from
   the tag-namespace prefix (``agent:backend`` -> ``agent``) with an
   ``untagged`` fallback for items that carry no tags.
2. **Inter-memory relationships** — a first-class, versioned data model
   (nodes = domains, edges = relationships) that is the stable INPUT CONTRACT
   for issue #68 (the graph-view UI). The contract is guarded by
   :data:`SCHEMA_VERSION`; bumping it is the only way the wire shape may change.
3. **Policy cohesion** — recurring ``constraint:`` / ``decision:`` /
   ``preference:`` themes aggregated *across* items. This coexists with and
   extends :class:`core.policy.PolicyEngine` promotion: promotion moves
   individual items up tiers, cohesion observes recurring themes and surfaces
   them as a higher-level view. The two are orthogonal — this module never
   promotes, demotes, or otherwise mutates policy state.

Design constraints (see ``docs/domain-policy-cohesion.md``):

* Pure functions, no global mutable state, no file/network I/O.
* Read-only over ``list[dict]`` memory items; tolerant of missing/extra keys.
* Deterministic, insertion-ordered outputs (stable for snapshot/graph tests).
* Backward compatible with existing tag/layer metadata — no migration.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from core.policy import PolicyEngine

#: Version of the issue-#68 relationship contract. Bumping this constant is the
#: only sanctioned way to change the node/edge wire shape; #68 reads
#: ``DomainGraph.schema_version`` to guard compatibility.
SCHEMA_VERSION: int = 1

#: Cap on SAME_LAYER edges emitted per layer. SAME_LAYER is a weak signal, so
#: it is chained (item N <-> item N+1) rather than fully connected to keep the
#: graph bounded for #68 instead of exploding to O(n^2) edges.
_SAME_LAYER_PER_LAYER_CAP: int = 64

#: Tag namespaces that represent policy themes for cohesion aggregation.
_POLICY_PREFIXES: tuple[str, ...] = ("constraint", "decision", "preference")

#: Slug used when an item carries no tags from which to derive a domain.
_UNTAGGED_KEY: str = "untagged"


class RelationKind(str, Enum):
    """Closed set of inter-memory edge kinds.

    The set is intentionally closed so issue #68 can switch exhaustively on the
    edge kind without a catch-all branch.
    """

    SHARES_TAG = "shares_tag"
    SAME_LAYER = "same_layer"
    PROMOTION_LINEAGE = "promotion_lineage"
    CO_DOMAIN = "co_domain"
    REFERENCES = "references"


@dataclass(frozen=True)
class Domain:
    """A derived grouping of memories sharing cohesion signals (a graph node)."""

    key: str
    label: str
    member_ids: tuple[str, ...]
    layers: tuple[str, ...]
    tags: tuple[str, ...]
    cohesion_score: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable plain dict (the #68 wire form)."""
        return {
            "key": self.key,
            "label": self.label,
            "member_ids": list(self.member_ids),
            "layers": list(self.layers),
            "tags": list(self.tags),
            "cohesion_score": self.cohesion_score,
        }


@dataclass(frozen=True)
class MemoryRelationship:
    """A directed-or-undirected edge between two memories (a graph edge)."""

    source_id: str
    target_id: str
    kind: RelationKind
    weight: float = 1.0
    directed: bool = False
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable plain dict (the #68 wire form).

        ``kind`` serializes to its enum *string value* (e.g. ``"shares_tag"``),
        not ``"RelationKind.SHARES_TAG"``.
        """
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind.value,
            "weight": self.weight,
            "directed": self.directed,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class DomainGraph:
    """Top-level serializable structure issue #68 consumes.

    Nodes are :class:`Domain` instances; edges are :class:`MemoryRelationship`
    instances. ``schema_version`` mirrors :data:`SCHEMA_VERSION` at build time.
    """

    schema_version: int
    domains: tuple[Domain, ...]
    relationships: tuple[MemoryRelationship, ...]
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable plain dict with nested nodes/edges."""
        return {
            "schema_version": self.schema_version,
            "domains": [d.to_dict() for d in self.domains],
            "relationships": [r.to_dict() for r in self.relationships],
            "generated_at": self.generated_at,
        }


@dataclass(frozen=True)
class PolicyCohesion:
    """A higher-level policy cluster aggregated across recurring memories."""

    theme: str
    member_ids: tuple[str, ...]
    layers: tuple[str, ...]
    recurrence: int
    suggested_layer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable plain dict."""
        return {
            "theme": self.theme,
            "member_ids": list(self.member_ids),
            "layers": list(self.layers),
            "recurrence": self.recurrence,
            "suggested_layer": self.suggested_layer,
        }


# --------------------------------------------------------------------------- #
# Internal helpers (pure, read-only)
# --------------------------------------------------------------------------- #
def _item_id(item: dict[str, Any]) -> str | None:
    """Return the item's id, or ``None`` when absent/empty."""
    value = item.get("id")
    return value if value else None


def _item_tags(item: dict[str, Any]) -> list[str]:
    """Return the item's tags as a list, tolerating missing/non-list values."""
    tags = item.get("tags")
    if not isinstance(tags, (list, tuple)):
        return []
    return [t for t in tags if isinstance(t, str)]


def _namespace(tag: str) -> str:
    """Return the namespace prefix of a tag (``agent:backend`` -> ``agent``)."""
    return tag.split(":", 1)[0] if ":" in tag else tag


# --------------------------------------------------------------------------- #
# Domain derivation
# --------------------------------------------------------------------------- #
def derive_domains(items: list[dict]) -> list[Domain]:
    """Group memory items into domains, read-only over the item dicts.

    The primary derivation key is the tag-namespace prefix. Items with no tags
    fall back to a single ``untagged`` domain so derivation never crashes.
    Output order is insertion-ordered by first appearance of each domain key.
    """
    # Preserve insertion order of domain keys across the item list.
    members: dict[str, list[str]] = {}
    layers: dict[str, list[str]] = {}
    tags: dict[str, list[str]] = {}

    for item in items:
        item_id = _item_id(item)
        if item_id is None:
            continue
        layer = item.get("layer")
        item_tags = _item_tags(item)

        keys_for_item: list[str]
        if item_tags:
            keys_for_item = []
            for tag in item_tags:
                key = _namespace(tag)
                if key not in keys_for_item:
                    keys_for_item.append(key)
                tags.setdefault(key, [])
                if tag not in tags[key]:
                    tags[key].append(tag)
        else:
            keys_for_item = [_UNTAGGED_KEY]

        for key in keys_for_item:
            members.setdefault(key, [])
            if item_id not in members[key]:
                members[key].append(item_id)
            layers.setdefault(key, [])
            if isinstance(layer, str) and layer not in layers[key]:
                layers[key].append(layer)

    domains: list[Domain] = []
    for key, member_ids in members.items():
        domains.append(
            Domain(
                key=key,
                label=key.replace("_", " ").replace("-", " ").title(),
                member_ids=tuple(member_ids),
                layers=tuple(layers.get(key, [])),
                tags=tuple(tags.get(key, [])),
                cohesion_score=_cohesion_score(member_ids, tags.get(key, [])),
            )
        )
    return domains


def _cohesion_score(member_ids: list[str], domain_tags: list[str]) -> float:
    """Return a cohesion score in [0, 1].

    A solo member has full cohesion (nothing competes with it). Otherwise the
    score is the ratio of distinct contributing tags to members, clamped to
    [0, 1]: a domain whose members all hang off one shared tag scores low on
    that ratio's inverse, while many distinct tags relative to members reads as
    a tighter, more specific cluster.
    """
    member_count = len(member_ids)
    if member_count <= 1:
        return 1.0
    tag_count = max(1, len(domain_tags))
    return round(min(1.0, tag_count / member_count), 4)


# --------------------------------------------------------------------------- #
# Relationship derivation
# --------------------------------------------------------------------------- #
def derive_relationships(
    items: list[dict],
    domains: list[Domain] | None = None,
) -> list[MemoryRelationship]:
    """Derive inter-memory edges, read-only over the item dicts.

    Emits, in this deterministic order:

    * ``SHARES_TAG`` — between items that share at least one exact tag.
    * ``SAME_LAYER`` — chained within each layer (bounded, not fully connected).
    * ``CO_DOMAIN`` — between members of the same derived domain.

    Edges are undirected, carry a weight in [0, 1], and never self-loop. When
    ``domains`` is ``None`` it is derived internally so ``CO_DOMAIN`` edges are
    always available to callers.
    """
    edges: list[MemoryRelationship] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(source: str, target: str, kind: RelationKind, weight: float, evidence: tuple[str, ...]) -> None:
        if source == target:
            return
        signature = (source, target, kind.value)
        if signature in seen:
            return
        seen.add(signature)
        edges.append(
            MemoryRelationship(
                source_id=source,
                target_id=target,
                kind=kind,
                weight=round(max(0.0, min(1.0, weight)), 4),
                directed=False,
                evidence=evidence,
            )
        )

    indexed = [(_item_id(it), it) for it in items]
    indexed = [(iid, it) for iid, it in indexed if iid is not None]

    # SHARES_TAG: pairwise over items sharing an exact tag.
    for pos, (sid, src) in enumerate(indexed):
        src_tags = set(_item_tags(src))
        if not src_tags:
            continue
        for tid, tgt in indexed[pos + 1:]:
            shared = src_tags & set(_item_tags(tgt))
            if shared:
                weight = len(shared) / max(len(src_tags), len(set(_item_tags(tgt))))
                _add(sid, tid, RelationKind.SHARES_TAG, weight, tuple(sorted(shared)))

    # SAME_LAYER: chain within each layer, bounded by a per-layer cap.
    by_layer: dict[str, list[str]] = {}
    for iid, it in indexed:
        layer = it.get("layer")
        if isinstance(layer, str):
            by_layer.setdefault(layer, []).append(iid)
    for layer, ids in by_layer.items():
        capped = ids[:_SAME_LAYER_PER_LAYER_CAP]
        for left, right in zip(capped, capped[1:]):
            _add(left, right, RelationKind.SAME_LAYER, 0.25, (layer,))

    # CO_DOMAIN: members of the same derived domain.
    if domains is None:
        domains = derive_domains(items)
    for domain in domains:
        ids = list(domain.member_ids)
        for left, right in zip(ids, ids[1:]):
            _add(left, right, RelationKind.CO_DOMAIN, domain.cohesion_score, (domain.key,))

    return edges


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
def build_domain_graph(items: list[dict]) -> DomainGraph:
    """Assemble the full :class:`DomainGraph` from raw memory item dicts.

    ``generated_at`` is an ISO-8601 UTC timestamp produced via
    ``datetime.datetime.now(datetime.timezone.utc)`` (never the deprecated
    ``utcnow``), so it is timezone-aware.
    """
    domains = derive_domains(items)
    relationships = derive_relationships(items, domains)
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return DomainGraph(
        schema_version=SCHEMA_VERSION,
        domains=tuple(domains),
        relationships=tuple(relationships),
        generated_at=generated_at,
    )


# --------------------------------------------------------------------------- #
# Policy cohesion aggregation (coexists with promotion, never replaces it)
# --------------------------------------------------------------------------- #
def aggregate_policy_cohesion(
    items: list[dict],
    policy: PolicyEngine | None = None,
) -> list[PolicyCohesion]:
    """Aggregate recurring policy themes across items, read-only.

    Clusters items tagged ``constraint:*`` / ``decision:*`` / ``preference:*``
    by their exact theme tag and counts recurrence. Inputs are never mutated.

    When a ``policy`` is supplied, it is used **read-only**: only
    :meth:`PolicyEngine.get_next_layer` is consulted to compute a
    ``suggested_layer`` for each cluster (based on the cluster's first-seen
    layer). The policy is never asked to promote or demote, and any error it
    raises (e.g. on an unknown layer) degrades to ``suggested_layer=None``.
    """
    themes: dict[str, list[str]] = {}
    theme_layers: dict[str, list[str]] = {}

    for item in items:
        item_id = _item_id(item)
        if item_id is None:
            continue
        layer = item.get("layer")
        for tag in _item_tags(item):
            if _namespace(tag) in _POLICY_PREFIXES:
                themes.setdefault(tag, [])
                if item_id not in themes[tag]:
                    themes[tag].append(item_id)
                theme_layers.setdefault(tag, [])
                if isinstance(layer, str) and layer not in theme_layers[tag]:
                    theme_layers[tag].append(layer)

    clusters: list[PolicyCohesion] = []
    for theme, member_ids in themes.items():
        layers = theme_layers.get(theme, [])
        suggested = _suggested_layer(policy, layers)
        clusters.append(
            PolicyCohesion(
                theme=theme,
                member_ids=tuple(member_ids),
                layers=tuple(layers),
                recurrence=len(member_ids),
                suggested_layer=suggested,
            )
        )
    return clusters


def _suggested_layer(policy: PolicyEngine | None, layers: list[str]) -> str | None:
    """Read-only consultation of the promotion chain for a cluster.

    Returns ``None`` when no policy is supplied, the cluster has no layer, or
    the policy reports no next layer / raises for the cluster's layer.
    """
    if policy is None or not layers:
        return None
    try:
        return policy.get_next_layer(layers[0])
    except Exception:
        return None
