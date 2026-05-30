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
the audit log, and the event bus all run normally. After
[#88](https://github.com/woogiekim/mnemos/issues/88) inverted the
transcript-capture path to a paragraph-level blacklist, the distillation
queue ingests substantive paragraphs from every assistant turn instead of
just the narrow marker-line whitelist — the pool of raw inputs the cohesion
and policy aggregators see is materially richer, which improves the
signal-to-noise of derived domains and policy themes without requiring any
change to the distillation contract or `backup.SCHEMA_VERSION`.

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

## Automatic distillation

> Operator guide for issue
> [#87](https://github.com/woogiekim/mnemos/issues/87) — automatic
> distillation runs on the existing `post-capture` in-process event seam and
> at the end of `mnemos consolidate`. It re-uses the same idempotent
> `compute_*_plan` → `apply_*_plan` pipeline documented above, with the
> single difference that auto-distill skips single-source plans (a one-member
> domain or policy conveys no aggregation value).

### When it fires

- **Every N captures** (default `25`). The gateway maintains a
  captures-since-last-distill counter in a small sidecar file (see below).
  When `gateway.capture(...)` crosses the threshold, auto-distill fires
  synchronously, the counter resets, and `last_distill_at` is stamped with
  the current UTC ISO timestamp.
- **End of `gateway.consolidate()`** — every `mnemos consolidate` run ends
  with one automatic distill (independent of the counter), then resets the
  counter so the next post-capture trigger lands exactly N captures later.

Both paths catch and log every exception via `core.observability.log_auto_distill`
(`event="auto_distill"` in `.agent/observability.jsonl`); `capture()` and
`consolidate()` never break because of a distill failure.

### Configuration

```yaml
storage:
  distillation:
    enabled: true            # default; set false to opt out entirely
    interval_captures: 25    # default; positive integer required
```

Both keys are optional. A missing `storage.distillation:` block, or a
missing key inside it, falls back to the defaults shown above. Invalid
values are tolerant — `enabled` must be a YAML bool (anything else falls
back to `true`), and `interval_captures` must be a positive non-bool
integer (non-int, `<= 0`, or a YAML `true`/`false` falls back to `25`).

**Default-on behavior on upgrade.** Projects that do not edit `mnemos.yml`
pick up automatic distillation the first time the new gateway code runs.
To opt out:

```yaml
storage:
  distillation:
    enabled: false
```

With `enabled: false`, neither the post-capture subscriber nor the
end-of-`consolidate` distill fires, and the `~/.mnemos/.distill-state.json`
sidecar is never written. The event-bus handler count for `post-capture`
is unchanged from the pre-#87 baseline.

### State file — `~/.mnemos/.distill-state.json`

A tiny JSON sidecar tracks the threshold counter:

```json
{
  "captures_since_last_distill": 7,
  "last_distill_at": "2025-12-01T12:34:56.789012+00:00"
}
```

- **Location.** Always under the user's HOME (`Path.home() / ".mnemos"`).
  Resolution respects `Path.home()` overrides, so the pytest `isolate_home`
  fixture redirects the sidecar to the test's tmp HOME automatically.
- **Outside the wiki tree.** The sidecar lives under `~/.mnemos/`, not under
  `wiki/` or `.agent/`, so it is never synced to a remote and never staged
  in a backup archive. The on-disk memory format is unchanged.
- **Atomic rewrite.** Writes use `tempfile.NamedTemporaryFile` in the same
  directory plus `os.replace`, so a concurrent reader never observes a
  partial write and a crashed writer never leaves the sidecar in a corrupt
  state.
- **Safe to delete.** If the file is missing, unreadable, contains non-JSON,
  or carries an unexpected schema, the gateway treats it as zero state — the
  next capture rebuilds the counter to `1`. You can safely delete the
  sidecar at any time; the next event recreates it.

### Idempotency guarantee

Auto-distill re-uses the same skip-if-exists guard introduced in
[#84](https://github.com/woogiekim/mnemos/issues/84): the artifact id is
`uuid5(...)` over the sorted source set, so running 25 captures twice
yields the same artifact id on both fires and the second `apply_*_plan`
returns `applied=False` (counted under `skipped` in the report) instead of
writing a duplicate. The same property holds for `distilled_into`
back-pointers — they are append-only and de-duplicated.

### Error swallow & observability

Every auto-distill fire — whether triggered by the post-capture subscriber
or by `consolidate()` — is wrapped in `try/except Exception`. On a
captured exception:

- The counter is **preserved**, not reset, so the next event retries the
  fire. (The consolidate path is unconditional, so it does not need to
  retry — the next sweep simply runs distill again.)
- An observability entry is appended with `success=False` and
  `error=str(exc)`. The capture call still returns the new memory id; the
  consolidate call still returns its promoted count.

Inspect the log with:

```bash
mnemos audit --events auto_distill
# or
grep '"event":"auto_distill"' .agent/observability.jsonl
```

Each entry carries:

- `trigger`: `"post-capture"` or `"consolidate"`.
- `success`: `true` or `false`.
- `interval`: the configured `interval_captures` at fire time.
- `counter_before`: the counter value observed before the fire (only set
  for `post-capture` events).
- `domains_applied` / `policies_applied`: how many new artifacts the fire
  produced (zero on a quiet store or on the `success=false` path).
- `error`: `str(exc)` when `success=false`, empty string otherwise.

### What auto-distill skips

Auto-distill **skips single-source plans** — both for domains and policies
— so a quiet store with a single untagged item is not polluted with a
1-member "untagged" domain artifact on the very first capture. The
explicit `mnemos distill domains apply` / `mnemos distill policies apply`
CLI is **unaffected**: when the operator runs it deliberately, every
plan returned by `compute_*_plan` is still applied, including 1-source
ones.
