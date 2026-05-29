# Domain / Policy Cohesion abstraction (issue #67)

This document defines the **domain / policy cohesion** abstraction that mnemos
layers over the memories it already stores, and — most importantly — specifies
the **stable, versioned inter-memory relationship data model** that issue #68
(the domain-relationship graph-view UI) consumes as its input contract.

It is a *design contract*, not a tutorial. The authoritative implementation is
`core/cohesion.py`; the authoritative tests are `tests/test_cohesion.py`. When
this document and the code disagree about the wire shape, the code plus a
`SCHEMA_VERSION` bump win — see [Versioning](#versioning).

## Goals and non-goals

mnemos classifies memories by `tag` and `layer`, and `core/policy.py` performs
layer promotion (`session -> project -> global`). It has no concept of a
*domain* (a cohesive cluster of related memories) nor any *inter-memory
relationship* graph. Issue #67 introduces that higher-level abstraction as a
**read-only derivation** over already-stored memories.

**Goals**

1. Document how memories derive/classify into domains.
2. Define a first-class, versioned inter-memory relationship data model
   (nodes = domains, edges = relationships) as the stable INPUT CONTRACT for #68.
3. Define how *policy cohesion* coexists with and extends `core/policy.py`
   promotion — without replacing promotion.
4. Preserve full backward compatibility with existing tag/layer metadata and
   memory files. No migration.

**Non-goals**

- The graph-view UI itself (that is issue #68).
- Any change to `MemoryStore`, frontmatter schema, `core/layers.py`,
  `core/contracts.py`, or to promotion thresholds / `PolicyEngine` behavior.
- Any file/network I/O, CLI wiring, or persistence. The module is pure.

## Input: the memory item dict

All derivation is **read-only** over the loosely-typed `dict[str, Any]` memory
item shape mnemos parses from YAML frontmatter:

| key            | type        | required | used by                         |
|----------------|-------------|----------|---------------------------------|
| `id`           | `str`       | yes\*    | node membership, edge endpoints |
| `layer`        | `str`       | no       | `layers`, `SAME_LAYER`, policy  |
| `tags`         | `list[str]` | no       | domain key, `SHARES_TAG`, policy|
| `content`      | `str`       | no       | reserved (untagged fallback)    |
| `created_at`, `stage`, `quality_score`, ... | various | no | ignored by cohesion |

\* Items without a usable `id` are silently skipped — they cannot be graph
nodes or edge endpoints. Every function tolerates missing/extra keys, empty
tag lists, unknown layers, and items with no content. **No key is added or
required**; existing stores and files keep working unchanged.

## The #68 relationship data model (the contract)

This is the stable, documented, versioned contract. It is implemented as frozen
dataclasses in `core/cohesion.py`, each with a `to_dict()` that returns a
plain, JSON-serializable dict (the wire form).

### `SCHEMA_VERSION`

```python
SCHEMA_VERSION: int  # == 1
```

A module-level constant. `DomainGraph.schema_version` mirrors it at build time.
**Bumping `SCHEMA_VERSION` is the only sanctioned way the contract may change.**
#68 reads `schema_version` to guard compatibility before interpreting nodes and
edges.

### `RelationKind` — closed edge-kind enum

```python
class RelationKind(str, Enum):
    SHARES_TAG = "shares_tag"
    SAME_LAYER = "same_layer"
    PROMOTION_LINEAGE = "promotion_lineage"
    CO_DOMAIN = "co_domain"
    REFERENCES = "references"
```

A `str` enum so it JSON-serializes to its value (`"shares_tag"`), and a
**closed** set so #68 can `switch` exhaustively on edge kind with no catch-all
branch. New kinds require a `SCHEMA_VERSION` bump.

### `Domain` — a graph node

```python
@dataclass(frozen=True)
class Domain:
    key: str                       # stable slug (tag namespace, or "untagged")
    label: str                     # human label derived from key
    member_ids: tuple[str, ...]    # member memory ids (insertion-ordered)
    layers: tuple[str, ...]        # layers represented in the domain
    tags: tuple[str, ...]          # contributing tags
    cohesion_score: float          # 0..1
```

`to_dict()` →
```json
{"key": "...", "label": "...", "member_ids": [...], "layers": [...],
 "tags": [...], "cohesion_score": 0.5}
```

### `MemoryRelationship` — a graph edge

```python
@dataclass(frozen=True)
class MemoryRelationship:
    source_id: str
    target_id: str
    kind: RelationKind
    weight: float = 1.0            # 0..1
    directed: bool = False
    evidence: tuple[str, ...] = ()
```

`to_dict()` →
```json
{"source_id": "a", "target_id": "b", "kind": "shares_tag",
 "weight": 1.0, "directed": false, "evidence": ["..."]}
```

`kind` serializes to its **string value** (`"shares_tag"`), never
`"RelationKind.SHARES_TAG"`. Edges never self-loop (`source_id != target_id`)
and never duplicate the same `(source, target, kind)` triple.

### `DomainGraph` — the top-level structure #68 consumes

```python
@dataclass(frozen=True)
class DomainGraph:
    schema_version: int
    domains: tuple[Domain, ...]              # nodes
    relationships: tuple[MemoryRelationship, ...]  # edges
    generated_at: str                        # ISO-8601 UTC, timezone-aware
```

`to_dict()` produces a fully nested, JSON-serializable dict:
```json
{
  "schema_version": 1,
  "domains": [ { ...Domain.to_dict()... } ],
  "relationships": [ { ...MemoryRelationship.to_dict()... } ],
  "generated_at": "2026-05-29T12:00:00+00:00"
}
```

`generated_at` is produced with `datetime.datetime.now(datetime.timezone.utc)`
(never the deprecated `utcnow`), so it is timezone-aware and round-trips through
`datetime.fromisoformat`.

### Entry point

```python
def build_domain_graph(items: list[dict]) -> DomainGraph
```

Assembles the whole graph from raw memory item dicts:
`nodes == derive_domains(items)`, `edges == derive_relationships(items, nodes)`,
`schema_version == SCHEMA_VERSION`.

## Domain derivation / classification

```python
def derive_domains(items: list[dict]) -> list[Domain]
```

- **Primary key:** the *tag-namespace prefix* — the substring before the first
  `:` of each tag (`agent:backend` → `agent`, `project:mnemos` → `project`).
  An item with multiple tag namespaces contributes to multiple domains.
- **Fallback:** items with no tags land in a single `untagged` domain, so
  derivation never crashes on bare items.
- **Membership** (`member_ids`), **layers**, and **tags** are accumulated per
  domain in **insertion order** of first appearance, making output
  deterministic and snapshot-stable.
- **`cohesion_score`** is in `[0, 1]`: a solo-member domain scores `1.0`;
  otherwise it is `min(1, distinct_tags / members)` — a lightweight,
  monotonic proxy for "how tightly the members hang together". The exact
  formula is an implementation detail and may be refined within `SCHEMA_VERSION`
  as long as the `[0, 1]` range and determinism hold.
- Empty input → `[]`.

## Relationship derivation

```python
def derive_relationships(items, domains=None) -> list[MemoryRelationship]
```

Emits edges in this deterministic order:

1. **`SHARES_TAG`** — between any two items sharing at least one exact tag;
   `weight = |shared| / max(|tags_a|, |tags_b|)`, `evidence = sorted shared tags`.
2. **`SAME_LAYER`** — items in the same `layer`, **chained** (item *N* ↔ item
   *N+1*) rather than fully connected, and capped per layer
   (`_SAME_LAYER_PER_LAYER_CAP`). SAME_LAYER is a weak signal; chaining keeps
   the graph bounded for #68 instead of exploding to O(n²) edges.
3. **`CO_DOMAIN`** — between consecutive members of the same derived domain;
   `weight = domain.cohesion_score`, `evidence = (domain.key,)`. When `domains`
   is `None` it is derived internally so CO_DOMAIN edges are always available.

All edges are undirected (`directed=False`), carry `weight ∈ [0, 1]`, never
self-loop, and are de-duplicated by `(source, target, kind)`.

> `PROMOTION_LINEAGE` and `REFERENCES` are part of the enumerated contract so
> #68 can render them, and are reserved for future derivation passes
> (e.g. explicit `references:<id>` tags or store-recorded promotion history).
> Reserving the enum members now means adding their derivation later does **not**
> require a `SCHEMA_VERSION` bump — only adding a brand-new kind does.

## Policy cohesion — coexists with, never replaces, promotion

```python
def aggregate_policy_cohesion(items, policy: "PolicyEngine | None" = None)
        -> list[PolicyCohesion]
```

```python
@dataclass(frozen=True)
class PolicyCohesion:
    theme: str                     # the exact policy tag, e.g. "constraint:api"
    member_ids: tuple[str, ...]
    layers: tuple[str, ...]
    recurrence: int                # == len(member_ids)
    suggested_layer: str | None = None
```

- Clusters items whose tags fall in the policy namespaces `constraint:*`,
  `decision:*`, `preference:*`, keyed by the **exact** theme tag, counting
  `recurrence`. Non-policy tags (e.g. `agent:backend`) are ignored.
- **Never mutates inputs** and performs no I/O.
- The optional `policy` argument is used **read-only**: only
  `PolicyEngine.get_next_layer(layer)` is consulted to compute
  `suggested_layer` from the cluster's first-seen layer. The engine is never
  asked to promote or demote. Any error it raises (e.g. on an unknown layer)
  degrades to `suggested_layer=None`.

### Why this does not replace promotion

Promotion and cohesion are **orthogonal**:

| Concern   | `core/policy.py` promotion              | `aggregate_policy_cohesion`           |
|-----------|-----------------------------------------|---------------------------------------|
| Subject   | one item at a time                      | recurring themes *across* items       |
| Action    | moves an item up a tier (mutating)      | observes & surfaces a view (read-only)|
| State     | reads thresholds, advances lifecycle    | no state, no lifecycle change         |
| Direction | `session -> project -> global`          | not a tier move; `suggested_layer` is advisory only |

`core/policy.py` is **not modified** by issue #67. Cohesion is strictly
additive: a separate read-only aggregation that *reads* layer/promotion signals
to annotate a higher-level policy view, while promotion continues to own all
actual tier transitions.

## Backward compatibility

- The new module is import-isolated; no other `core/` module imports it at
  runtime, and it imports `PolicyEngine` only under `TYPE_CHECKING`.
- No new required frontmatter fields; no migration. Existing tag/layer metadata
  and memory files keep working unchanged.
- All derivation tolerates missing/extra keys, empty tag lists, unknown layers,
  and items with no content.

## Versioning

The contract is frozen behind `SCHEMA_VERSION` (currently `1`). Changes that
**require a bump**: adding/removing/renaming a `RelationKind` member, changing a
dataclass field name/type, or changing the `to_dict()` wire shape. Changes that
**do not require a bump**: refining `cohesion_score`/`weight` numerics within
`[0, 1]`, or adding derivation for an already-enumerated `RelationKind`
(`PROMOTION_LINEAGE`, `REFERENCES`). #68 must read `DomainGraph.schema_version`
and refuse to interpret a graph whose version it does not recognize.
