# Security & Privacy

> Scope: the mnemos persisted memory store and its observability output.
> This document covers what mnemos persists, where that data can travel, which
> fields are sensitive, and how a user controls deletion and export. It is the
> answer to issue #77 (parent epic [#65](#parent-epic)).
>
> Companion docs — cross-linked, not duplicated:
> [backup-restore.md](./backup-restore.md) (export/disaster-recovery, #75) and
> [remote-sync.md](./remote-sync.md) (remote git-sync hardening, #79).

## TL;DR

- mnemos stores memory as Markdown files on disk under two trees with very
  different exposure profiles: a **synced** `wiki/` tree (git-committed and
  pushable to a remote) and an **ephemeral** `.agent/` tree (gitignored, never
  pushed).
- The single rule that governs leakage: **anything written to a synced `wiki/`
  layer can reach a remote. Never put secrets in memory `content` or in
  `extra_metadata`.**
- The observability log was the one place where this rule was violated: it lived
  under `wiki/` and was therefore staged and pushed. That leak (Finding **F1**)
  is fixed — the log now lives under the gitignored `.agent/` tree and is
  local-only.

## 1. Sensitive-memory handling policy (AC1)

mnemos persists memory items as Markdown files with a YAML front-matter metadata
block. The body (`content`) and the metadata both land on disk verbatim. There
is no field-level encryption and no automatic secret redaction. Sensitivity is
therefore a function of **which layer** an item is captured into.

Policy:

- **Never capture secrets, credentials, tokens, API keys, or PII into a synced
  layer.** Synced layers (`project`, `global`, `entities`, `claims`, `topics` —
  see [§2](#2-local-data-exposure-boundaries-ac2)) are git-committed and can be
  pushed to a remote. Treat them as if they were already public.
- **Never inject secrets through `extra_metadata`.** The capture path merges any
  caller-supplied `extra_metadata` dict directly into the persisted front-matter
  (`core/gateway.py:518-519`). Whatever a caller puts there is serialized to disk
  and, for synced layers, travels to the remote. This is the open door called
  out in [§3](#3-persisted-metadata-field-inventory-ac3).
- **Ephemeral / working / session / transient captures are local-only** and are
  the correct home for anything short-lived or sensitive that must not leave the
  machine. They map under `.agent/` (`core/store.py:232-248`) and are gitignored
  (`.gitignore:16-18`).
- mnemos does not validate content for secrets. The boundary is operational, not
  enforced in code. The capturing agent or user is responsible for layer choice.

## 2. Local data exposure boundaries (AC2)

mnemos uses a **dual-track** data model. The two tracks have fundamentally
different exposure: one is pushed to a git remote, the other never leaves the
local working tree.

| Track | Layers | On-disk path | Git status | Can reach a remote? |
|---|---|---|---|---|
| **Synced** | `project`, `global`, `entities`, `claims`, `topics` | `wiki/projects`, `wiki/global`, `wiki/entities`, `wiki/claims`, `wiki/topics` (`core/layers.py:4-10`, `LAYER_STATIC_PATHS`) | tracked + committed | **Yes** — git-synced and pushable |
| **Ephemeral** | `ephemeral`, `working` | `.agent/runs/<run_id>/scratch`, `.agent/runs/<run_id>/working` (`core/store.py:232-239`) | gitignored (`.gitignore:16`) | No |
| **Ephemeral** | `session` | `.agent/sessions/<session_id>` (`core/store.py:240-243`) | gitignored (`.gitignore:17`) | No |
| **Ephemeral** | `transient` | `.agent/transient` (`core/layers.py:18`, `TRANSIENT_PATH`) | under `.agent/`; never staged by the wiki filter | No |

### The stage seam

The boundary is enforced at a single seam: the wiki stage filter
`MemoryStore._wiki_stage_filter` (`core/store.py:168-190`). On a sync commit, the
sentinel branch stages the **whole `wiki/` directory**
(`core/store.py:180-184`); any path outside `wiki/` is excluded
(`core/store.py:185-189`, "Decision 2", #69). Repo source is never committed by
the default backend.

The practical consequence: **anything that lands anywhere under `wiki/` is
synced**, even if it is not a memory item. That is exactly how the observability
log leaked before the F1 fix — see [§4](#4-observabilitylogreport-leakage-review-ac4).

When auto-push is enabled, committed `wiki/` content is pushed to the configured
remote. See [remote-sync.md](./remote-sync.md) for the remote-sync contract and
hardening (#79).

## 3. Persisted-metadata field inventory (AC3)

Every captured item carries a metadata block that is serialized to YAML
front-matter and persisted alongside the content. For synced layers, these
fields travel to the remote. The inventory below is the gateway capture metadata
(`core/gateway.py:503-519`) plus the durable contract fields
(`core/contracts.py:70-85`, `MemoryMetadata`).

| Field | Source | Sensitivity | Notes |
|---|---|---|---|
| `id` / `item_id` | gateway:504; contract:74 | low | logical memory id |
| `layer` | gateway:505; contract:75 | low | which track the item lives in |
| `stage` | gateway:506; contract:76 | low | lifecycle stage (e.g. `stored`) |
| `created_at` / `updated_at` | gateway:507; contract:80-81 | low | ISO-8601 timestamps |
| `access_count` | gateway:508 | low | usage counter |
| `quality_score` | gateway:509; contract:85 | low | ranking heuristic |
| `tags` | gateway:510; contract:78 | **low–medium** | free-form; do not encode secrets in tag names |
| `run_id` | gateway:511 | low | UUID4 (`core/gateway.py:159`) — opaque, not user-identifying |
| `session_id` | gateway:512 | low | UUID4 — opaque |
| `content_hash` | gateway:516 | low | dedup hash of content |
| `provenance` | contract:79 | **medium** | free-form dict; callers control its contents |
| `source` | contract:83 | **medium** | free-form origin string |
| `workflow_id` | contract:82 | low–medium | workflow correlation id |
| `trust_level`, `confidence` | contract:77, 84 | low | trust heuristics |
| **`extra_metadata` (merged)** | gateway:518-519 | **HIGH — open door** | arbitrary caller-supplied keys merged verbatim into front-matter; the one field that can persist anything |

Key takeaway: `session_id` / `run_id` are UUID4 and low-sensitivity. The risk
surface is **`extra_metadata`** (and to a lesser degree `provenance` / `source` /
`tags`), because callers can inject arbitrary keys that are then persisted and —
for synced layers — pushed. See the AC1 policy: secrets never go in `content` or
`extra_metadata`.

## 4. Observability / log / report leakage review (AC4)

### What the observability log records

The observability logger (`core/observability.py`) appends one JSON object per
line to an `observability.jsonl` file. Recorded events include:

- **Hook and programmatic searches** — store the full `keywords` list verbatim
  (`core/observability.py:117-133` `log_hook_search`,
  `core/observability.py:150-165` `log_search`). Result references are stored as
  lightweight `{id, score}` pairs (`core/observability.py:127-131, 160-163`), not
  full content.
- `session_id` and `agent` on every entry (`_base`), plus event-specific fields:
  surfaced `memory_id`, `layer`, and `tags` on captures
  (`core/observability.py:135-148`), GC counts, and `result_count`.

The **raw search keywords are the highest-signal data** in the log: a query like
`"prod database password reset"` is recorded literally. The `audit` table view
truncates keyword display to 30 chars, but `audit --json` surfaces them verbatim
(Finding **F2**, below).

### Finding F1 — the log was inside the synced `wiki/` tree (HIGH, fixed)

Before this change, the logger hard-coded its path to
`{repo_root}/wiki/observability.jsonl`. Because the wiki stage filter
([§2](#the-stage-seam)) stages the entire `wiki/` directory on a sync commit, the
observability log — raw search keywords and all — was committed and, with
auto-push enabled, **pushed to the remote**. The inconsistency that confirmed the
intent: the backup module already excludes `observability.jsonl` as "not a memory
item" (`core/backup.py:16-18`), yet git-sync was including it. The two channels
disagreed.

**Fix:** the observability log was relocated out of the synced `wiki/` tree into
the gitignored `.agent/` tree. The log now lives at
`.agent/observability.jsonl` (`core/observability.py`), and the read mirror in
`core/context.render_promotion_block` (`core/context.py`) was repointed in
lockstep. After the fix:

- The log is **local-only**. It is under `.agent/`, which the wiki stage filter
  never stages and which is excluded from git-sync — so it is never committed and
  never pushed.
- `mnemos audit` / `mnemos stats` continue to read and aggregate it; no audit or
  stats output schema changed.
- The backup exclusion (`core/backup.py:16-18`) remains correct: backups snapshot
  only `wiki/<layer>/*.md` memory items, so the observability log was never in a
  backup and still is not.

### Retention caveat (F2)

The observability log is **append-only with no rotation or redaction**. Even now
that it is local-only, raw query terms are retained indefinitely on the local
machine. Operators who treat search queries as sensitive should periodically
truncate or delete `.agent/observability.jsonl`. A keyword length cap or
count-only logging mode is a documented future hardening option (F2), not part of
this change.

## 5. User-controlled deletion & export (AC5)

### Deletion

mnemos exposes two delete verbs with different guardrails:

| Command | Guard | Behavior |
|---|---|---|
| `mnemos delete <id>` | **none** — unconditional (`core/cli.py:713-731`) | removes a memory item immediately; intended for transient ephemeral/session cleanup |
| `mnemos forget <id>` | requires **archived** stage; confirms unless `--force` / `--yes` (`core/cli.py:694-710`) | hard-delete that goes through the archive lifecycle and a policy check (`validate_forget`) |

Both ultimately call into the gateway, which unlinks the on-disk file and removes
the item from the full-text-search index:

- `gateway.delete` (`core/gateway.py:865-884`): `self._store.delete(...)` then
  `self._fts.remove(...)`, plus an audit log entry.
- `gateway.forget` (`core/gateway.py:849-859`): validates `forget` policy, then
  the same store-delete + FTS-remove + audit entry.

### Git-history caveat (synced layers)

Deletion removes a file from the **working tree** and the FTS index. For a
**synced `wiki/` layer**, that is not the same as erasing it everywhere: the
content remains in the repository's git history (and on any remote it was pushed
to) until history is rewritten. Local deletion + a new commit does not retroact
past commits. To fully purge a synced memory from a remote you must rewrite git
history on the remote. (Ephemeral `.agent/` layers are never committed, so this
caveat does not apply to them.)

### Export

mnemos has **no dedicated `export` verb**. The read/extract surfaces are:

- `mnemos read <id>` (`core/cli.py:523`) — read a single item's content and
  metadata.
- `mnemos audit --json` — emit the observability event stream as JSON (note: this
  surfaces raw search keywords verbatim — see F2).

For a full, portable snapshot of the persistent memory store, use the backup
mechanism (`mnemos backup` → gzip tar of `wiki/<layer>/*.md`). The backup format,
its `.agent/` exclusion, and the restore contract are documented in
[backup-restore.md](./backup-restore.md) (#75) — this document does not duplicate
that material.

## Related work

- **#69 — git-sync.** Established the default-backend wiki-only stage filter
  ("Decision 2") that this document's [§2](#the-stage-seam) boundary relies on.
- **#75 — backup / restore.** Export-via-backup and disaster recovery:
  [backup-restore.md](./backup-restore.md).
- **#76 — data safety.** Broader data-safety guarantees for the memory store.
- **#79 — remote-sync hardening.** Remote push/pull contract and conflict
  handling: [remote-sync.md](./remote-sync.md).

## Parent epic

This review and the F1 fix close issue **#77**, part of parent epic **#65**
(mnemos hardening — security, privacy, sync, and data-safety guarantees for the
persisted memory store).
