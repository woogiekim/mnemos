# Memory Compaction

> Operator guide for issue
> [#81](https://github.com/woogiekim/mnemos/issues/81) — similar-memory
> detection, semantic compression with lineage audit, and the
> `mnemos compact` CLI surface.

> Scope: deterministic-by-default merging of similar memories that have
> drifted into duplication over time. Lineage (a back-link from every
> source to the merged memory) is preserved by additive YAML
> front-matter — no schema bump, no new dependency. An opt-in LLM
> summariser is exposed but always falls back to the deterministic
> path on failure, so reproducibility is the contract.

> Companion docs — cross-linked, not duplicated:
>
> - [Memory Inspection UI](./memory-inspection.md) — sibling read-only
>   surface; compaction *writes*, inspection *reads*. Use inspect to
>   visualize the post-merge state.
> - [Backup & Restore](./backup-restore.md) — the additive front-matter
>   fields (`sources`, `compaction_method`, `superseded_by`) ride along
>   in `make_backup` / `restore_backup` without bumping
>   `SCHEMA_VERSION`.
> - [Remote Sync](./remote-sync.md) — merged memories that land in a
>   `wiki/`-mapped layer round-trip through git-sync just like every
>   other memory; `.agent/`-layer sources are archived locally only.
> - [Security & Privacy](./security-privacy.md) — the deterministic
>   summariser is offline-only and emits no network traffic. The
>   `--summarizer=llm` flag is opt-in and uses the existing
>   `anthropic` client.

## TL;DR

```bash
# 1. Review (dry-run) — print groups + proposed merged content, write nothing.
mnemos compact review --threshold 0.7

# 2. Apply — write the merged memory, archive each source, set
#    superseded_by back-pointers.  Deterministic by default.
mnemos compact apply --threshold 0.7

# 3. Audit any source after merge — walk the lineage back from the
#    merged memory's `sources` array.
mnemos compact restore-source <source-uuid>

# 4. Machine-readable candidate list (alias for review --format=json).
mnemos compact merge-candidates --threshold 0.7 --layer project
```

The default summariser is **lossless-of-meaning by construction**: it
produces an ordered union of unique lines from every source, followed
by a `## Sources` audit header listing every source id, layer, and
created_at. Same inputs → same output, regardless of caller order.

## CLI surface

```text
mnemos compact review [--threshold FLOAT] [--layer LAYER ...]
                      [--format text|json]

mnemos compact apply  [--threshold FLOAT] [--layer LAYER ...]
                      [--summarizer deterministic|llm] [--forget-sources]

mnemos compact restore-source SOURCE_ID

mnemos compact merge-candidates [--threshold FLOAT] [--layer LAYER ...]
```

| Flag | Default | Meaning |
|---|---|---|
| `--threshold` | `0.7` | Jaccard similarity threshold for grouping. Items with score `< threshold` are not paired. |
| `--layer` | (all) | Repeatable. Restrict source items to the named layer(s). |
| `--format` | `text` | `review` only. `json` emits a single JSON payload (the same shape `merge-candidates` returns). |
| `--summarizer` | `deterministic` | `apply` only. `llm` uses the existing `anthropic` client; on **any** failure (no key, network, schema mismatch) the call logs a warning and falls back to deterministic. |
| `--forget-sources` | off | `apply` only. After merge, hard-delete sources instead of leaving them archived. Use only after you have confirmed lineage via `restore-source`. |

## Acceptance criterion mapping

Issue #81 lists eight ACs. Each is anchored by named tests in this
release and a specific behaviour in the implementation:

| AC | Behaviour | Anchored by |
|---|---|---|
| 1. Detect similar memories | Jaccard over NFKC-normalised tokens with optional n-gram | `core/similarity.py:jaccard_similarity`, `tests/test_similarity.py::TestJaccardSimilarity` |
| 2. Rank merge candidates | `find_similar_pairs` returns score-annotated pairs, deterministically ordered | `core/similarity.py:find_similar_pairs`, `tests/test_similarity.py::TestFindSimilarPairs` |
| 3. Reviewable before apply | `mnemos compact review` and `merge-candidates` print groups + proposed content, write nothing | `tests/test_cli_compact.py::TestCompactReview::test_review_prints_groups_and_writes_nothing`, `tests/test_compaction.py::TestComputeMergePlan::test_pure_no_writes` |
| 4. Deterministic compression | Default summariser is ordered unique-line union; same inputs → same output | `core/compaction.py:deterministic_summary`, `tests/test_compaction.py::TestDeterministicSummary::test_is_deterministic_across_input_orderings` |
| 5. Lifecycle-aware target layer | `derive_merged_layer` picks the highest layer present via `PolicyEngine.get_next_layer` — no hard-coded layer name | `tests/test_compaction_lifecycle.py::TestMergedLayerFollowsPolicy` |
| 6. Supersede semantics | Sources are archived (stage flip) AND back-referenced (`superseded_by`); hard delete is opt-in via `--forget-sources` | `core/compaction.py:apply_merge_plan`, `tests/test_cli_compact.py::TestCompactApply::test_apply_forget_sources_hard_deletes` |
| 7. Audit trail | Merged memory carries `sources: [...]`; `mnemos compact restore-source` walks back to the original | `tests/test_compaction_lifecycle.py::TestAuditTrailReconstruction::test_every_source_recoverable_via_restore_source` |
| 8. Lifecycle + compression tests | Sync + backup round-trip preserves all three additive fields | `tests/test_compaction_sync_roundtrip.py`, `tests/test_compaction_backup_roundtrip.py` |

## Similarity threshold tuning

`--threshold` is the **Jaccard** similarity cutoff — the fraction of
shared tokens vs. the total token union. Sensible starting points:

- **0.9** — only near-identical memories merge. Use this for an
  initial cautious pass on a wiki you've never compacted.
- **0.7** (default) — merges paraphrases that share most content but
  differ in word order or minor phrasing. Recommended for routine
  cleanup of session captures that have drifted.
- **0.5** — merges loosely-related memories. Likely to over-collapse
  distinct topics that share boilerplate vocabulary. Use only after
  reviewing the output of `mnemos compact review` and convincing
  yourself the groups are real.

Always run `review` first at the threshold you intend to apply at.
The dry-run prints the proposed merged content so you can sanity-check
each group before the destructive `apply`.

## Deterministic vs LLM summariser

| Summariser | When to use | What you trade |
|---|---|---|
| `deterministic` (default) | Always safe; reproducible offline; preserves every distinct content line. | Reads like a concatenation, not prose. |
| `llm` | When the merge corpus is verbose and a coherent narrative summary is preferred. | Requires network + an `ANTHROPIC_API_KEY`; on any failure (no key, network error, malformed response) the call falls back to deterministic with a logged warning — never raises. |

The deterministic path always appends a `## Sources` audit header so
lineage is recoverable from the merged file alone, regardless of which
summariser produced the body.

## Supersede vs forget semantics

By default `mnemos compact apply` is **non-destructive**:

- Each source's stage is flipped to `archived`.
- Each source gains a `superseded_by: <merged-id>` front-matter field.
- The source markdown file remains on disk and is readable via
  `mnemos compact restore-source <id>` and `mnemos read <id>`.

This is the "soft delete + audit trail" mode. To upgrade to hard
delete, pass `--forget-sources`:

```bash
mnemos compact apply --threshold 0.7 --forget-sources
```

`--forget-sources` removes the source markdown files entirely. The
merged memory's `sources: [...]` array is still authoritative — but
walking the array via `restore-source` will now return
`FileNotFoundError`. **Only forget after confirming the merged
content is sufficient via `mnemos compact restore-source` on every
source.**

## Audit trail / lineage examples

After a merge, the merged memory's front-matter carries:

```yaml
---
id: <merged-uuid>
layer: project
stage: stored
sources:
  - <source-1-uuid>
  - <source-2-uuid>
compaction_method: jaccard-merge-v1
content_hash: ...
---
<merged content body>

## Sources

- <source-1-uuid> (layer=session, created_at=2026-05-29T10:00:00Z)
- <source-2-uuid> (layer=session, created_at=2026-05-29T11:00:00Z)
```

Each source's front-matter gains the back-pointer:

```yaml
---
id: <source-1-uuid>
layer: session
stage: archived
superseded_by: <merged-uuid>
---
<original source content unchanged>
```

To walk the trail:

```bash
# Read the merged memory; the 'sources' field is the lineage anchor.
mnemos read <merged-uuid>

# Print each source's archived snapshot.
mnemos compact restore-source <source-1-uuid>
```

## Sync + backup round-trip notes

The three additive front-matter fields (`sources`,
`compaction_method`, `superseded_by`) are plain YAML strings /
lists. They:

- Round-trip through `git push` / `git pull` via the default
  `MemoryStore` backend's sync engine ([#69](https://github.com/woogiekim/mnemos/issues/69),
  [#79](https://github.com/woogiekim/mnemos/issues/79)).
- Round-trip through `mnemos backup` / `mnemos restore` without a
  `core/backup.py:SCHEMA_VERSION` bump (the manifest schema is
  unchanged; per-item front-matter is the per-memory contract).
- Are **additive**: a build that predates this feature can read a
  memory with these fields and simply ignore them.

A merge whose sources live in `.agent/` layers (session / working /
ephemeral) lands the merged memory in a `wiki/`-mapped layer chosen
by PolicyEngine (typically `project`). The merged memory therefore
syncs to the remote and the lineage audit survives across hosts.
Sources that lived in `.agent/` layers are archived locally only —
the merged memory's `sources` array still records their ids, so the
audit trail is reconstructable from any host that has the merged
memory + the originating host's archive.

## Operational checklist before running `apply`

1. Run `mnemos compact review --threshold X` and read every group's
   proposed merged content. Verify the union makes sense.
2. If any group is wrong, raise the threshold and re-review.
3. If a group is right but the proposed body is missing a critical
   line, file an issue with the group ids — the deterministic
   summariser should not lose content. (If it does, that is a bug.)
4. Run `mnemos compact apply --threshold X` (do **not** pass
   `--forget-sources` on the first pass).
5. Spot-check with `mnemos compact restore-source <source-id>` on at
   least one source per group.
6. Only after the above, consider `--forget-sources` for a follow-up
   pass.

The whole flow is idempotent: a second `apply` will skip sources
already archived + superseded, because `find_similar_pairs` filters
them out by design.
