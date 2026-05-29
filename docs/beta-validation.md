# Beta Validation Harness

> Operator guide for issue
> [#82](https://github.com/woogiekim/mnemos/issues/82) (parent epic
> [#65](https://github.com/woogiekim/mnemos/issues/65)) — a deterministic,
> virtual-clock harness that exercises mnemos over a *simulated* multi-day
> timeline under real-usage-like workflows and reports evidence on contextual
> continuity, retrieval relevance stability, lifecycle consistency, and
> degradation/recovery.

> Scope: a re-runnable, fully deterministic beta-validation driver. It drives
> the *real* `MemoryGateway` / store on an isolated tmp home (never a mock) so
> the evidence is meaningful, simulates a multi-day timeline with a seeded
> virtual clock so it runs in seconds with no wall-clock or network dependence,
> and computes five acceptance-criteria metrics with the standard library only
> (no numpy/pandas/matplotlib). Two runs with the same seed produce a
> byte-identical report.

> Companion docs — cross-linked, not duplicated:
>
> - [Backup & Restore](./backup-restore.md) (issue #75) — AC5 (degradation +
>   recovery) snapshots the store with `make_backup` and recovers a partial
>   loss with `restore_backup`. That guide owns the archive format and the
>   restore policy; this harness only *exercises* it.
> - [Remote Sync](./remote-sync.md) (issue #79) — the harness can model a
>   sync-interruption fault; the sync engine's conflict and round-trip
>   semantics live in that guide.
> - [Memory Compaction](./memory-compaction.md) (issue #81) — the lifecycle
>   workflow the harness drives includes the compaction surface; that guide
>   owns the merge/lineage contract.

---

## TL;DR

Run the harness and write a JSON report:

```bash
mnemos beta-run --days 14 --seed 42 --output beta-report.json --json
```

Or print a human-readable summary to stdout:

```bash
mnemos beta-run --days 14 --seed 42
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--days N` | `14` | Number of simulated days to run. |
| `--seed S` | `42` | RNG seed — fixes every reproducible choice and the virtual clock. |
| `--output PATH` | (stdout) | Write the report to a file instead of stdout. |
| `--json` | off | Emit JSON (sorted keys) instead of the markdown summary. |

The harness builds its own isolated, real mnemos repo under a temp directory
for the run — it never touches your live store.

---

## How it works

1. **Virtual clock.** `VirtualClock(start_epoch, seed)` exposes `now()`,
   `advance(days=…, hours=…)`, and `iso()`. Simulated time is injected without
   any production signature change: each capture passes
   `extra_metadata={"created_at": clock.iso()}` (the gateway applies the
   override after setting its own `created_at`), and the harness policy pins
   every layer's `promotion.age_hours` to `0.0` so promotion is clock-
   independent. GC staleness scoring uses `compute_garbage_score(..., now=…)`.
2. **Seeded workflow.** A single `random.Random(seed)` drives a reproducible
   per-day mix of capture / search / promote / GC scoring / lifecycle scan
   against the real gateway.
3. **Deterministic report.** Non-deterministic fields (backup `generated_at` /
   `source_host`, any sync commit timestamps) are never stored in the report,
   so same-seed runs are byte-identical.

---

## The five metrics

| AC | Metric | Definition |
|----|--------|------------|
| **AC1** | Multi-day runs documented | This harness IS the documented, re-runnable multi-day driver; the captured sample below is a real run. |
| **AC2** | Contextual continuity (`recall`) | Fixed anchors captured on the first simulated day are probed by keyword on a later day. `recall = surfaced_anchors / total_anchors`. A recall of `1.0` means every early memory is still retrievable. |
| **AC3** | Retrieval relevance stability (`stability`) | A fixed target memory is probed under a fixed query on every day as the store grows. `stability = 1 − normalized_rank_variance`, where the normalized variance is the population variance of the observed ranks divided by the square of the worst observed rank. A perfectly stable rank yields `1.0`. |
| **AC4** | Lifecycle-invariant consistency (`violations`) | After each day the harness scans all persistent layers and counts violations: a stage not in `PolicyEngine.VALID_STAGES`, an orphaned `superseded_by` pointer, or a layer that fails `PolicyEngine.get_next_layer`. A healthy run reports `violations == 0`. |
| **AC5** | Degradation + recovery | The harness snapshots the store (`make_backup`), injects a partial-loss fault (deletes half the persistent items), asserts it **detects** the degradation (live count < snapshot count), then **recovers** via `restore_backup` and asserts the post-recovery state equals the pre-fault state (`degradation_detected` AND `recovery_consistent`). |

---

## Captured sample run

The following is a **real** `mnemos beta-run --days 14 --seed 42` invocation
(not invented numbers). The full JSON report is tracked at
[`examples/beta-run-sample.json`](./examples/beta-run-sample.json).

Human summary (`mnemos beta-run --days 14 --seed 42`):

```text
# Beta Validation Report (seed=42, days=14)

- Total captures: 46

## AC2 — Contextual continuity
- Recall: 1.0000 (4/4 anchors surfaced)

## AC3 — Retrieval relevance stability
- Stability: 1.0000 over 14 samples
- Observed ranks: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
- Normalized rank variance: 0.0000

## AC4 — Lifecycle-invariant consistency
- Violations: 0 (invalid_stage=0, orphaned_supersede=0, layer_monotonicity=0)
- Days scanned: 14, items scanned: 46

## AC5 — Degradation + recovery
- Degradation detected: True
- Recovery consistent: True
- Item counts pre/post-fault/post-recovery: 46/23/46
```

JSON report (`--json`, excerpt — full file in `examples/`):

```json
{
  "continuity": { "recall": 1.0, "surfaced_anchors": 4, "total_anchors": 4 },
  "days": 14,
  "lifecycle": {
    "days_scanned": 14,
    "invalid_stage_count": 0,
    "items_scanned": 46,
    "layer_monotonicity_violation_count": 0,
    "orphaned_supersede_count": 0,
    "violations": 0
  },
  "recovery": {
    "degradation_detected": true,
    "post_fault_item_count": 23,
    "post_recovery_item_count": 46,
    "pre_fault_item_count": 46,
    "recovery_consistent": true
  },
  "relevance": {
    "normalized_rank_variance": 0.0,
    "rank_variance": 0.0,
    "samples": 14,
    "stability": 1.0
  },
  "seed": 42,
  "start_epoch": 1700000000.0,
  "total_captures": 46
}
```

Reading the sample: every anchor captured on day 0 is still surfaced on the
final day (continuity recall `1.0`); the probe target holds rank 1 across all
14 days (stability `1.0`); no lifecycle invariant was violated across the
timeline (`violations == 0`); and the injected partial loss (46 → 23 items) was
detected and fully recovered (23 → 46).

---

## Determinism guarantee

`mnemos beta-run --days N --seed S` is reproducible from `(N, S)` alone. Two
runs with the same seed produce a byte-identical normalized report — this is
asserted by `tests/test_beta_harness.py::test_same_seed_byte_identical`. The
harness never reads wall-clock time and never touches the network.
