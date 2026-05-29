# Final-Memory Distillation

> Operator guide for issue
> [#84](https://github.com/woogiekim/mnemos/issues/84) — persisting derived
> domains and aggregated policies as managed, durable "final memory" artifacts
> with lineage, lifecycle-aware layers, deterministic idempotent re-builds, and
> the `mnemos distill` CLI surface.

> Scope: the *persistence / management* layer that issue #67's read-only
> cohesion derivation lacks. #67 derives domains and policy-cohesion themes
> **on demand** and never writes them; #84 turns those views into real,
> lifecycle-managed memory artifacts you can search, back up, and sync — without
> changing any public API on the cohesion / gateway / store / policy / backup
> modules and without bumping `backup.SCHEMA_VERSION`. The new per-item
> front-matter is additive YAML, so older archives still restore.

> Companion docs — cross-linked, not duplicated:
>
> - [Memory Compaction](./memory-compaction.md) — issue #81. Distillation reuses
>   #81's lineage *pattern* (`derive_merged_layer`, a `## Sources` audit block,
>   a `restore-source` audit walk) but inverts the destructiveness contract:
>   compaction **supersedes and archives** sources (`superseded_by`), whereas
>   distillation is **additive** — sources keep their stage/layer and gain only
>   an append-only `distilled_into` back-pointer. See
>   [Lineage model](#lineage-model-distilled_into-vs-81-superseded_by) below.
> - [Unified Inspection UI](./unified-inspection-ui.md) — issue #83. Distilled
>   artifacts are ordinary memories, so they appear in the inspect/graph views
>   automatically. #84 changes no UI / `unifiedview` code.
> - [Domain Graph View](./domain-graph-view.md) — issue #68. The cohesion
>   `Domain` / `PolicyCohesion` shapes distillation persists are the same
>   read-only structures #68 visualizes; `cohesion_schema_version` is recorded
>   on every artifact so a future #68 schema bump is detectable.

## TL;DR

```bash
# 1. Review (dry-run) — print the would-be domain artifacts, write nothing.
mnemos distill domains review

# 2. Apply — persist each domain artifact and append a non-destructive
#    `distilled_into` back-pointer to every source. Idempotent.
mnemos distill domains apply

# 3. Same review/apply pair for aggregated policy themes.
mnemos distill policies review
mnemos distill policies apply

# 4. Standalone aggregate_policy_cohesion exposure (read-only; writes nothing).
mnemos distill cohesion --format json

# 5. Audit a source — walk forward to the artifacts it contributed to.
mnemos distill restore-source <source-uuid>
```

`review` is the dry-run gate: it computes the deterministic artifact ids and
bodies and prints them but writes nothing. `apply` persists them and echoes one
line per artifact:

```text
distilled: <artifact_id> ← <n> sources (layer=<derived-layer>)
```

A second `apply` over the same store is a true no-op — it prints
`distilled: <artifact_id> (exists — skipped)` and adds no duplicate artifact and
no duplicate back-link.

## What distillation is (vs derive #67 vs compact #81)

| | derive (#67) | compact (#81) | distill (#84) |
|---|---|---|---|
| Writes to the store? | No (read-only view) | Yes (merged memory) | Yes (artifact memory) |
| Source disposition | untouched | **archived + superseded** | **untouched + annotated** |
| Back-pointer on source | — | `superseded_by` (single) | `distilled_into` (append-only list) |
| Trigger | any read | similarity threshold | tag-namespace cohesion |
| Re-run safety | n/a | dedup short-circuit | deterministic id + skip-if-exists |

Distillation reads the eligible memory pool, runs the existing
`core.cohesion.derive_domains` / `core.cohesion.aggregate_policy_cohesion`
functions to produce the artifact *bodies*, and persists each result via
`gateway.capture(..., no_classify=True, item_id=<deterministic id>)` so FTS,
the audit log, and the event bus all run normally.

## CLI surface

```text
mnemos distill domains  review [--layer LAYER ...] [--format text|json]
mnemos distill domains  apply  [--layer LAYER ...]
mnemos distill policies review [--layer LAYER ...] [--format text|json]
mnemos distill policies apply  [--layer LAYER ...]
mnemos distill cohesion        [--layer LAYER ...] [--format text|json]
mnemos distill restore-source SOURCE_ID
```

`mnemos distill cohesion` is the standalone exposure of
`core.cohesion.aggregate_policy_cohesion`, which previously had no direct
command surface (it was reachable only via the #83 unified UI). It is read-only
and writes nothing; `--format json` emits the `PolicyCohesion.to_dict()` shape
verbatim (`theme`, `member_ids`, `layers`, `recurrence`, `suggested_layer`).

## The distill model

Each distilled artifact is a normal memory item carrying additive front-matter:

| Field | Meaning |
|---|---|
| `artifact_kind` | `"domain"` or `"policy"` — the discriminator. |
| `distillation_method` | `"domain-distill-v1"` / `"policy-distill-v1"`. |
| `sources` | sorted list of the source memory ids the artifact was built from. |
| `cohesion_schema_version` | `core.cohesion.SCHEMA_VERSION` at build time. |
| `domain_key` | (domain only) the cohesion domain key. |
| `policy_theme` / `recurrence` / `suggested_layer` | (policy only) the cluster's theme tag, recurrence count, and the PolicyEngine-suggested layer. |

Artifacts are also marker-tagged `distilled:domain` / `distilled:policy`. That
namespace is deliberately **outside** `core.cohesion._POLICY_PREFIXES`, so policy
cohesion never treats an artifact's own tag as a policy theme.

## Lineage model: `distilled_into` vs #81 `superseded_by`

This is the deliberate design difference from compaction:

- A distilled artifact carries `sources: [...]` (the forward lineage).
- Each source gains an **append-only** `distilled_into: [...]` list (the
  backward lineage). The list is merged with any existing value and deduped, so
  a source that contributes to several artifacts (e.g. an item tagged both
  `agent:backend` and `constraint:no-push` feeds a domain *and* a policy
  cluster) accumulates every artifact id by append — never overwrite.
- **Sources are never archived, superseded, moved, or forgotten.** Domains and
  policies are an *additive higher layer* over the originals; the originals
  remain first-class. This is why distillation uses `distilled_into` (additive)
  rather than #81's `superseded_by` (which archives the source).

Both directions are reconstructable: from an artifact, read `sources`; from a
source, read `distilled_into` and walk to each artifact.

## Idempotency & determinism

The artifact id is `uuid5(<fixed namespace>, "{kind}:{sorted source ids}")`, so:

- the **same** kind over the **same** source-set always yields the **same** id
  and the **same** body, regardless of input order;
- `apply` is guarded by a skip-if-exists probe (`store.read(artifact_id)`), so a
  second run writes no duplicate artifact and appends no duplicate
  `distilled_into` entry.

There is **no LLM dependency** in the default path — distillation bodies are
produced deterministically from the cohesion structures.

### Feedback-loop guard

The planners exclude any item already carrying an `artifact_kind` of `"domain"`
or `"policy"` from the source pool. A distilled artifact can therefore never
become a source of a later re-derivation, so repeated `apply` runs converge
instead of distilling prior artifacts into ever-higher artifacts.

## Lifecycle / layer derivation

The artifact's layer is **derived from its sources**, never hard-coded: it is
the highest layer present among the sources per the configured PolicyEngine
promotion chain (`core.compaction.derive_merged_layer`, reused — not
duplicated). A domain built from a `project` source and a `global` source lands
on `global`; a policy cluster spanning `session` + `project` lands on `project`.

## Audit & restore

```bash
mnemos distill restore-source <source-uuid>
```

prints the source's full front-matter as a YAML block (including its
`distilled_into` back-pointers) followed by the original markdown body. Use it
to verify, from any source, exactly which final-memory artifacts it fed.

## Sync & backup safety

The new fields are additive YAML front-matter, so they ride through unchanged:

- **git-sync** — a distilled artifact written into a `wiki/`-mapped layer and
  the `distilled_into` annotation on its sources survive the
  write → commit → push → fresh-clone read cycle (same default-backend path as
  #69/#79).
- **backup / restore** — the fields survive `make_backup` → `restore_backup`
  into a fresh root. **`backup.SCHEMA_VERSION` is NOT bumped** (it stays `1`);
  per-item front-matter is additive, so no manifest change is required and older
  archives still restore.
