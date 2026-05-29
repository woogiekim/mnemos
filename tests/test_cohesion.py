"""Tests for core.cohesion — domain/policy cohesion abstraction (issue #67).

These tests are derived solely from the PRD Input/Output Contract and the
"Behavioral contract test-writer must cover" bullet list. They drive the
read-only data-model scaffold that issue #68 (graph-view UI) consumes as its
stable input contract: nodes = domains, edges = relationships.

The repo enforces ``--cov-fail-under=100`` and ``filterwarnings=["error"]``,
so every line of ``core/cohesion.py`` must be exercised here with no warnings
emitted (in particular: ``generated_at`` must use timezone-aware UTC and never
the deprecated ``datetime.utcnow``).
"""
from __future__ import annotations

import datetime
import json

import pytest

from core import cohesion
from core.cohesion import (
    SCHEMA_VERSION,
    Domain,
    DomainGraph,
    MemoryRelationship,
    PolicyCohesion,
    RelationKind,
    aggregate_policy_cohesion,
    build_domain_graph,
    derive_domains,
    derive_relationships,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _item(item_id, *, tags=None, layer="session", content=""):
    """Build a dict-shaped memory item matching the stored frontmatter shape."""
    d = {"id": item_id, "layer": layer, "content": content}
    if tags is not None:
        d["tags"] = list(tags)
    return d


@pytest.fixture
def backend_items():
    return [
        _item("a", tags=["agent:backend", "project:mnemos"], layer="session"),
        _item("b", tags=["agent:backend"], layer="session"),
        _item("c", tags=["project:mnemos"], layer="project"),
    ]


# --------------------------------------------------------------------------- #
# Module-level constants & enum
# --------------------------------------------------------------------------- #
def test_schema_version_is_one():
    assert SCHEMA_VERSION == 1
    assert isinstance(SCHEMA_VERSION, int)


def test_relation_kind_is_str_enum_with_closed_set():
    assert issubclass(RelationKind, str)
    assert RelationKind.SHARES_TAG == "shares_tag"
    assert RelationKind.SAME_LAYER == "same_layer"
    assert RelationKind.PROMOTION_LINEAGE == "promotion_lineage"
    assert RelationKind.CO_DOMAIN == "co_domain"
    assert RelationKind.REFERENCES == "references"
    # Closed set so #68 can switch exhaustively.
    assert {k.value for k in RelationKind} == {
        "shares_tag",
        "same_layer",
        "promotion_lineage",
        "co_domain",
        "references",
    }


def test_relation_kind_serializes_to_string_value():
    assert json.dumps(RelationKind.SHARES_TAG) == '"shares_tag"'


# --------------------------------------------------------------------------- #
# Dataclass shape / immutability / to_dict
# --------------------------------------------------------------------------- #
def test_domain_is_frozen():
    d = Domain(
        key="agent",
        label="Agent",
        member_ids=("a",),
        layers=("session",),
        tags=("agent:backend",),
        cohesion_score=1.0,
    )
    with pytest.raises((AttributeError, Exception)):
        d.key = "other"  # type: ignore[misc]


def test_domain_to_dict_json_serializable():
    d = Domain(
        key="agent",
        label="Agent",
        member_ids=("a", "b"),
        layers=("session",),
        tags=("agent:backend",),
        cohesion_score=0.5,
    )
    out = d.to_dict()
    assert isinstance(out, dict)
    # Round-trips through json with no custom encoder.
    restored = json.loads(json.dumps(out))
    assert restored["key"] == "agent"
    assert restored["label"] == "Agent"
    assert restored["member_ids"] == ["a", "b"]
    assert restored["layers"] == ["session"]
    assert restored["tags"] == ["agent:backend"]
    assert restored["cohesion_score"] == 0.5


def test_memory_relationship_defaults_and_to_dict():
    rel = MemoryRelationship(source_id="a", target_id="b", kind=RelationKind.SHARES_TAG)
    assert rel.weight == 1.0
    assert rel.directed is False
    assert rel.evidence == ()
    out = rel.to_dict()
    restored = json.loads(json.dumps(out))
    assert restored["source_id"] == "a"
    assert restored["target_id"] == "b"
    # kind serializes to its enum string value, not "RelationKind.SHARES_TAG".
    assert restored["kind"] == "shares_tag"
    assert restored["weight"] == 1.0
    assert restored["directed"] is False
    assert restored["evidence"] == []


def test_memory_relationship_is_frozen():
    rel = MemoryRelationship(source_id="a", target_id="b", kind=RelationKind.SAME_LAYER)
    with pytest.raises((AttributeError, Exception)):
        rel.weight = 0.2  # type: ignore[misc]


def test_memory_relationship_with_evidence_and_directed():
    rel = MemoryRelationship(
        source_id="a",
        target_id="b",
        kind=RelationKind.REFERENCES,
        weight=0.25,
        directed=True,
        evidence=("a mentions b",),
    )
    out = rel.to_dict()
    assert out["directed"] is True
    assert out["evidence"] == ["a mentions b"]
    assert out["weight"] == 0.25


def test_domain_graph_to_dict_nested_serializable():
    domains = (
        Domain(
            key="agent",
            label="Agent",
            member_ids=("a",),
            layers=("session",),
            tags=("agent:backend",),
            cohesion_score=1.0,
        ),
    )
    rels = (
        MemoryRelationship(source_id="a", target_id="b", kind=RelationKind.SHARES_TAG),
    )
    graph = DomainGraph(
        schema_version=SCHEMA_VERSION,
        domains=domains,
        relationships=rels,
        generated_at="2026-05-29T00:00:00+00:00",
    )
    out = graph.to_dict()
    restored = json.loads(json.dumps(out))
    assert restored["schema_version"] == SCHEMA_VERSION
    assert isinstance(restored["domains"], list)
    assert restored["domains"][0]["key"] == "agent"
    assert isinstance(restored["relationships"], list)
    assert restored["relationships"][0]["kind"] == "shares_tag"
    assert restored["generated_at"] == "2026-05-29T00:00:00+00:00"


def test_domain_graph_is_frozen():
    graph = DomainGraph(
        schema_version=SCHEMA_VERSION,
        domains=(),
        relationships=(),
        generated_at="2026-05-29T00:00:00+00:00",
    )
    with pytest.raises((AttributeError, Exception)):
        graph.schema_version = 99  # type: ignore[misc]


def test_policy_cohesion_default_and_to_dict():
    pc = PolicyCohesion(
        theme="constraint",
        member_ids=("a", "b"),
        layers=("session", "project"),
        recurrence=2,
    )
    assert pc.suggested_layer is None
    out = pc.to_dict()
    restored = json.loads(json.dumps(out))
    assert restored["theme"] == "constraint"
    assert restored["member_ids"] == ["a", "b"]
    assert restored["layers"] == ["session", "project"]
    assert restored["recurrence"] == 2
    assert restored["suggested_layer"] is None


def test_policy_cohesion_is_frozen():
    pc = PolicyCohesion(theme="decision", member_ids=("a",), layers=("session",), recurrence=1)
    with pytest.raises((AttributeError, Exception)):
        pc.recurrence = 9  # type: ignore[misc]


def test_policy_cohesion_with_suggested_layer():
    pc = PolicyCohesion(
        theme="preference",
        member_ids=("a",),
        layers=("session",),
        recurrence=1,
        suggested_layer="project",
    )
    assert pc.to_dict()["suggested_layer"] == "project"


# --------------------------------------------------------------------------- #
# derive_domains
# --------------------------------------------------------------------------- #
def test_derive_domains_empty_input_returns_empty_list():
    assert derive_domains([]) == []


def test_derive_domains_groups_by_tag_namespace_prefix(backend_items):
    domains = derive_domains(backend_items)
    assert all(isinstance(d, Domain) for d in domains)
    by_key = {d.key: d for d in domains}
    # Two namespaces: agent and project.
    assert "agent" in by_key
    assert "project" in by_key
    # Items sharing the 'agent' prefix land in one domain.
    assert set(by_key["agent"].member_ids) == {"a", "b"}
    # Items sharing the 'project' prefix land in one domain.
    assert set(by_key["project"].member_ids) == {"a", "c"}


def test_derive_domains_membership_and_layers_and_tags(backend_items):
    by_key = {d.key: d for d in derive_domains(backend_items)}
    agent = by_key["agent"]
    assert "agent:backend" in agent.tags
    assert "session" in agent.layers
    assert 0.0 <= agent.cohesion_score <= 1.0


def test_derive_domains_cohesion_score_in_unit_interval(backend_items):
    for d in derive_domains(backend_items):
        assert 0.0 <= d.cohesion_score <= 1.0


def test_derive_domains_untagged_items_fall_back_without_crashing():
    items = [_item("x", content="some free text about caching"), _item("y")]
    domains = derive_domains(items)
    # Never crashes; untagged items land in a fallback domain.
    assert isinstance(domains, list)
    all_members = {m for d in domains for m in d.member_ids}
    assert {"x", "y"} <= all_members


def test_derive_domains_is_deterministic_insertion_ordered(backend_items):
    first = [d.key for d in derive_domains(backend_items)]
    second = [d.key for d in derive_domains(backend_items)]
    assert first == second
    # 'agent' is seen before 'project' on item "a"'s tag list.
    assert first.index("agent") < first.index("project")


def test_derive_domains_tolerates_missing_keys():
    # No 'tags', no 'layer', no 'content' — must not raise.
    domains = derive_domains([{"id": "z"}])
    assert isinstance(domains, list)
    assert any("z" in d.member_ids for d in domains)


def test_derive_domains_item_without_id_is_tolerated():
    domains = derive_domains([{"tags": ["agent:x"]}])
    assert isinstance(domains, list)


# --------------------------------------------------------------------------- #
# derive_relationships
# --------------------------------------------------------------------------- #
def test_derive_relationships_shares_tag_edges(backend_items):
    rels = derive_relationships(backend_items)
    shares = [r for r in rels if r.kind == RelationKind.SHARES_TAG]
    # a & b share agent:backend.
    pairs = {(r.source_id, r.target_id) for r in shares}
    assert ("a", "b") in pairs or ("b", "a") in pairs


def test_derive_relationships_same_layer_edges(backend_items):
    rels = derive_relationships(backend_items)
    same_layer = [r for r in rels if r.kind == RelationKind.SAME_LAYER]
    # a & b are both 'session'.
    pairs = {frozenset((r.source_id, r.target_id)) for r in same_layer}
    assert frozenset(("a", "b")) in pairs


def test_derive_relationships_co_domain_edges(backend_items):
    domains = derive_domains(backend_items)
    rels = derive_relationships(backend_items, domains)
    co = [r for r in rels if r.kind == RelationKind.CO_DOMAIN]
    assert co, "expected at least one CO_DOMAIN edge within a domain"
    for r in co:
        assert r.source_id != r.target_id


def test_derive_relationships_weights_in_unit_interval(backend_items):
    for r in derive_relationships(backend_items):
        assert 0.0 <= r.weight <= 1.0


def test_derive_relationships_no_self_edges(backend_items):
    for r in derive_relationships(backend_items):
        assert r.source_id != r.target_id


def test_derive_relationships_deterministic_ordering(backend_items):
    first = [(r.source_id, r.target_id, r.kind.value) for r in derive_relationships(backend_items)]
    second = [(r.source_id, r.target_id, r.kind.value) for r in derive_relationships(backend_items)]
    assert first == second


def test_derive_relationships_empty_input():
    assert derive_relationships([]) == []


def test_derive_relationships_single_item_has_no_edges():
    assert derive_relationships([_item("solo", tags=["agent:x"])]) == []


def test_derive_relationships_derives_domains_when_none_passed(backend_items):
    # Passing domains=None must internally derive them so CO_DOMAIN still appears.
    rels = derive_relationships(backend_items, None)
    assert any(r.kind == RelationKind.CO_DOMAIN for r in rels)


def test_derive_relationships_tolerates_missing_keys():
    items = [{"id": "p"}, {"id": "q"}]
    # No tags/layers — must not raise; may simply yield no tag/layer edges.
    rels = derive_relationships(items)
    assert isinstance(rels, list)


def test_derive_relationships_same_layer_is_bounded():
    # Many items in the same layer must not explode into O(n^2) unbounded edges.
    items = [_item(str(n), tags=[], layer="session") for n in range(50)]
    rels = derive_relationships(items)
    same_layer = [r for r in rels if r.kind == RelationKind.SAME_LAYER]
    # Bounded: far fewer than the full n*(n-1)/2 = 1225 pairs.
    assert len(same_layer) < 1225


# --------------------------------------------------------------------------- #
# build_domain_graph
# --------------------------------------------------------------------------- #
def test_build_domain_graph_returns_domain_graph(backend_items):
    graph = build_domain_graph(backend_items)
    assert isinstance(graph, DomainGraph)
    assert graph.schema_version == SCHEMA_VERSION


def test_build_domain_graph_nodes_are_domains_edges_are_relationships(backend_items):
    graph = build_domain_graph(backend_items)
    assert graph.domains == tuple(derive_domains(backend_items))
    assert all(isinstance(d, Domain) for d in graph.domains)
    assert all(isinstance(r, MemoryRelationship) for r in graph.relationships)


def test_build_domain_graph_generated_at_is_timezone_aware_iso8601(backend_items):
    graph = build_domain_graph(backend_items)
    parsed = datetime.datetime.fromisoformat(graph.generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.timedelta(0)


def test_build_domain_graph_to_dict_round_trips(backend_items):
    graph = build_domain_graph(backend_items)
    blob = json.dumps(graph.to_dict())
    restored = json.loads(blob)
    assert restored["schema_version"] == SCHEMA_VERSION
    assert "domains" in restored
    assert "relationships" in restored
    assert "generated_at" in restored


def test_build_domain_graph_empty_input():
    graph = build_domain_graph([])
    assert graph.domains == ()
    assert graph.relationships == ()
    assert graph.schema_version == SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# aggregate_policy_cohesion
# --------------------------------------------------------------------------- #
@pytest.fixture
def policy_items():
    return [
        _item("c1", tags=["constraint:api"], layer="session"),
        _item("c2", tags=["constraint:api"], layer="project"),
        _item("d1", tags=["decision:db"], layer="session"),
        _item("p1", tags=["preference:style"], layer="global"),
        _item("misc", tags=["agent:backend"], layer="session"),  # not a policy theme
    ]


def test_aggregate_policy_cohesion_clusters_by_theme(policy_items):
    result = aggregate_policy_cohesion(policy_items)
    assert all(isinstance(p, PolicyCohesion) for p in result)
    themes = {p.theme for p in result}
    # Only the policy-prefixed tags become themes.
    assert "constraint:api" in themes
    assert "decision:db" in themes
    assert "preference:style" in themes
    assert "agent:backend" not in themes


def test_aggregate_policy_cohesion_recurrence_counts(policy_items):
    by_theme = {p.theme: p for p in aggregate_policy_cohesion(policy_items)}
    assert by_theme["constraint:api"].recurrence == 2
    assert set(by_theme["constraint:api"].member_ids) == {"c1", "c2"}
    assert by_theme["decision:db"].recurrence == 1


def test_aggregate_policy_cohesion_never_mutates_inputs(policy_items):
    import copy

    snapshot = copy.deepcopy(policy_items)
    aggregate_policy_cohesion(policy_items)
    assert policy_items == snapshot


def test_aggregate_policy_cohesion_works_with_policy_none(policy_items):
    result = aggregate_policy_cohesion(policy_items, policy=None)
    assert isinstance(result, list)
    for p in result:
        assert p.suggested_layer is None


def test_aggregate_policy_cohesion_empty_input():
    assert aggregate_policy_cohesion([]) == []


def test_aggregate_policy_cohesion_tolerates_missing_keys():
    # Items without tags / layer must not raise.
    assert aggregate_policy_cohesion([{"id": "x"}]) == []


def test_aggregate_policy_cohesion_skips_items_without_id():
    # A policy-tagged item that lacks an id must be skipped (not crash, not
    # produce a phantom cluster keyed on a missing id).
    items = [
        {"tags": ["constraint:api"], "layer": "session"},  # no id -> skipped
        _item("c1", tags=["constraint:api"], layer="session"),
    ]
    result = aggregate_policy_cohesion(items)
    by_theme = {p.theme: p for p in result}
    assert by_theme["constraint:api"].member_ids == ("c1",)
    assert by_theme["constraint:api"].recurrence == 1


def test_aggregate_policy_cohesion_reads_policy_get_next_layer_only(policy_items):
    """When given a PolicyEngine it must only READ get_next_layer — never
    promote/demote/mutate."""

    class ReadOnlySpy:
        def __init__(self):
            self.get_next_layer_calls = []

        def get_next_layer(self, layer):
            self.get_next_layer_calls.append(layer)
            return "project" if layer == "session" else None

        def __getattr__(self, name):
            # Any attribute other than get_next_layer is a forbidden access.
            raise AssertionError(f"forbidden policy access: {name}")

    spy = ReadOnlySpy()
    result = aggregate_policy_cohesion(policy_items, policy=spy)
    # It consulted get_next_layer at least once.
    assert spy.get_next_layer_calls
    # And produced a suggested_layer for at least one cluster.
    assert any(p.suggested_layer is not None for p in result)


def test_aggregate_policy_cohesion_is_deterministic(policy_items):
    first = [(p.theme, p.recurrence) for p in aggregate_policy_cohesion(policy_items)]
    second = [(p.theme, p.recurrence) for p in aggregate_policy_cohesion(policy_items)]
    assert first == second


def test_aggregate_policy_cohesion_suggested_layer_none_when_top_layer():
    """A cluster whose representative layer is already the top layer gets
    suggested_layer=None even with a policy supplied."""

    class TopLayerPolicy:
        def get_next_layer(self, layer):
            return None  # already at top

    items = [_item("g1", tags=["constraint:x"], layer="global")]
    result = aggregate_policy_cohesion(items, policy=TopLayerPolicy())
    assert result[0].suggested_layer is None


def test_aggregate_policy_cohesion_handles_policy_raising_on_unknown_layer():
    """If the policy raises on an unknown layer (real PolicyEngine behavior),
    aggregation must degrade to suggested_layer=None rather than propagate."""

    class RaisingPolicy:
        def get_next_layer(self, layer):
            raise ValueError(f"unknown layer {layer}")

    items = [_item("u1", tags=["constraint:y"], layer="weird-layer")]
    result = aggregate_policy_cohesion(items, policy=RaisingPolicy())
    assert result[0].suggested_layer is None


# --------------------------------------------------------------------------- #
# Module-level read-only / no-IO guarantee
# --------------------------------------------------------------------------- #
def test_module_has_no_module_level_mutable_state():
    # SCHEMA_VERSION is the only "constant" surface; ensure functions are pure
    # by verifying repeated calls produce equal results on equal inputs.
    items = [_item("a", tags=["agent:x"]), _item("b", tags=["agent:x"])]
    assert derive_domains(items) == derive_domains(items)
    assert derive_relationships(items) == derive_relationships(items)
    assert aggregate_policy_cohesion(items) == aggregate_policy_cohesion(items)


def test_cohesion_module_exposes_public_api():
    for name in (
        "SCHEMA_VERSION",
        "RelationKind",
        "Domain",
        "MemoryRelationship",
        "DomainGraph",
        "PolicyCohesion",
        "derive_domains",
        "derive_relationships",
        "build_domain_graph",
        "aggregate_policy_cohesion",
    ):
        assert hasattr(cohesion, name)
