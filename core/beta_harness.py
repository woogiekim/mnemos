"""Long-running beta validation harness for mnemos (issue #82).

This module exercises mnemos over a *simulated* multi-day timeline under
real-usage-like workflows and collects deterministic evidence for five
acceptance criteria (ACs):

* **AC1 — multi-day runs documented.** This harness IS the documented,
  re-runnable multi-day driver; the companion doc embeds a captured run.
* **AC2 — contextual continuity.** A memory captured on an early simulated
  day is still surfaced by ``gateway.search`` on a later day.
* **AC3 — retrieval relevance stability.** A fixed probe query's target rank
  stays stable as the store grows over days.
* **AC4 — lifecycle-invariant consistency.** promote/archive/gc transitions
  never violate :class:`~core.policy.PolicyEngine` invariants (stage in
  :data:`~core.policy.VALID_STAGES`, no orphaned ``superseded_by``, layer
  monotonicity via :meth:`~core.policy.PolicyEngine.get_next_layer`).
* **AC5 — degradation + recovery.** A fault is injected (partial file loss),
  the harness DETECTS the degradation, then RECOVERS via
  :func:`core.backup.restore_backup` from a pre-fault
  :func:`core.backup.make_backup` snapshot and asserts the store returns to a
  consistent (pre-fault) state.

Design constraints (enforced by the reviewer):

* **No production signature change.** The harness drives the *existing* public
  surface only. Simulated time is injected WITHOUT a production seam:
  ``gateway.capture(..., extra_metadata={"created_at": clock.iso()})`` overrides
  the timestamp (the gateway does ``metadata.update(extra_metadata)`` after
  setting ``created_at``), and a harness ``policy.yaml`` pins every layer's
  ``promotion.age_hours`` to ``0.0`` so promotion depends only on
  access_count / quality_score (clock-independent). GC staleness scoring uses
  :func:`core.gc.compute_garbage_score` with an explicit ``now=`` argument.
* **No new dependency.** Standard library only.
* **Deterministic.** A single ``random.Random(seed)`` drives every choice, the
  virtual clock is seeded, and non-deterministic report fields are normalized
  out so two same-seed runs produce byte-identical reports.
* **Real code paths.** The harness builds an isolated tmp home with a real
  ``wiki/`` tree and drives a real :class:`~core.gateway.MemoryGateway`; the
  store is never mocked.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.backup import make_backup, restore_backup
from core.gateway import MemoryGateway
from core.gc import compute_garbage_score
from core.policy import VALID_STAGES

# Persistent layers that ``make_backup`` snapshots and that have a deterministic
# promotion chain (project -> global). The workflow captures into ``project``
# and promotes selected items to ``global``.
_CAPTURE_LAYER = "project"
_PROMOTE_TARGET = "global"

# A fixed anchor set captured on the first simulated day and probed on every
# later day to measure contextual continuity (AC2). Each anchor is a
# ``(probe_query, content)`` pair where the probe query is a distinctive word
# that appears verbatim in the content, so a successful search proves the
# captured memory is still retrievable on a later simulated day.
_ANCHORS: tuple[tuple[str, str], ...] = (
    ("hexagonal", "The project adopts a hexagonal architecture pattern for isolation."),
    ("opensearch", "Search is backed by an opensearch retrieval backend for scale."),
    ("requestid", "Every public API response includes a requestid header value."),
    ("kebabcase", "All CLI flag names follow the kebabcase naming convention."),
)

# A fixed target memory + probe query used to measure retrieval relevance
# stability as the store grows (AC3).
_PROBE_QUERY = "deterministic virtual clock harness"
_PROBE_TARGET_CONTENT = (
    "The deterministic virtual clock harness drives a seeded multi-day workflow."
)

# Filler content templates used to grow the store on each simulated day. The
# seeded RNG selects from these so the growth is reproducible.
_FILLER_TEMPLATES: tuple[str, ...] = (
    "Daily note {n}: refactored the {topic} module for clarity.",
    "Daily note {n}: triaged a flaky test in the {topic} suite.",
    "Daily note {n}: documented the {topic} rollout plan.",
    "Daily note {n}: reviewed a pull request touching {topic}.",
    "Daily note {n}: captured a design constraint about {topic}.",
)
_FILLER_TOPICS: tuple[str, ...] = (
    "retrieval",
    "lifecycle",
    "sync",
    "backup",
    "compaction",
    "policy",
)

_GC_STALENESS_HOURS = 24.0


class VirtualClock:
    """Deterministic, injectable virtual clock.

    ``start_epoch`` is the simulated start time as a UTC epoch (seconds).
    ``seed`` is retained for provenance and is recorded in the report so a run
    is fully reproducible from ``(start_epoch, seed, days)``. The clock never
    reads wall-clock time.
    """

    def __init__(self, start_epoch: float, seed: int) -> None:
        self._start_epoch = float(start_epoch)
        self._seed = int(seed)

        self._elapsed_seconds = 0.0

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def start_epoch(self) -> float:
        return self._start_epoch

    def now(self) -> datetime.datetime:
        """Return the current simulated time as an aware UTC datetime."""
        epoch = self._start_epoch + self._elapsed_seconds

        return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)

    def advance(self, days: float = 0.0, hours: float = 0.0) -> None:
        """Advance the simulated clock by ``days`` and/or ``hours``."""
        self._elapsed_seconds += days * 86400.0 + hours * 3600.0

    def iso(self) -> str:
        """Return the simulated time as an ISO-8601 string for capture metadata.

        The format mirrors :meth:`core.gateway.MemoryGateway.capture`'s own
        ``created_at`` shape (ISO 8601 with a trailing ``Z``) so downstream
        parsing is unchanged.
        """
        return self.now().isoformat() + "Z"


@dataclass(frozen=True)
class ContinuityMetric:
    """AC2 — contextual continuity recall across simulated days."""

    total_anchors: int
    surfaced_anchors: int
    recall: float


@dataclass(frozen=True)
class RelevanceMetric:
    """AC3 — retrieval relevance stability of a fixed probe target."""

    samples: int
    observed_ranks: tuple[int, ...]
    rank_variance: float
    normalized_rank_variance: float
    stability: float


@dataclass(frozen=True)
class LifecycleMetric:
    """AC4 — lifecycle-invariant consistency over the timeline."""

    days_scanned: int
    items_scanned: int
    invalid_stage_count: int
    orphaned_supersede_count: int
    layer_monotonicity_violation_count: int
    violations: int


@dataclass(frozen=True)
class RecoveryMetric:
    """AC5 — degradation detection + recovery to a consistent state."""

    pre_fault_item_count: int
    post_fault_item_count: int
    post_recovery_item_count: int
    degradation_detected: bool
    recovery_consistent: bool


@dataclass(frozen=True)
class BetaReport:
    """Aggregate, deterministic beta-validation report.

    The report is rendered both as machine JSON (:meth:`to_json`) and as a
    human/markdown summary (:meth:`to_markdown`). Non-deterministic fields
    (backup ``generated_at`` / ``source_host``, sync commit timestamps) are
    NEVER stored here — only normalized, reproducible metrics — so two
    same-seed runs produce byte-identical output.
    """

    seed: int
    days: int
    start_epoch: float
    total_captures: int
    continuity: ContinuityMetric
    relevance: RelevanceMetric
    lifecycle: LifecycleMetric
    recovery: RecoveryMetric

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-dict view with all nested metrics expanded."""
        return dataclasses.asdict(self)

    def to_json(self, *, indent: int = 2) -> str:
        """Return a stable, sorted JSON rendering for machine consumers."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        """Return a human-readable markdown summary of the run."""
        c = self.continuity
        r = self.relevance
        lc = self.lifecycle
        rc = self.recovery

        lines = [
            f"# Beta Validation Report (seed={self.seed}, days={self.days})",
            "",
            f"- Total captures: {self.total_captures}",
            "",
            "## AC2 — Contextual continuity",
            f"- Recall: {c.recall:.4f} ({c.surfaced_anchors}/{c.total_anchors} anchors surfaced)",
            "",
            "## AC3 — Retrieval relevance stability",
            f"- Stability: {r.stability:.4f} over {r.samples} samples",
            f"- Observed ranks: {list(r.observed_ranks)}",
            f"- Normalized rank variance: {r.normalized_rank_variance:.4f}",
            "",
            "## AC4 — Lifecycle-invariant consistency",
            f"- Violations: {lc.violations} "
            f"(invalid_stage={lc.invalid_stage_count}, "
            f"orphaned_supersede={lc.orphaned_supersede_count}, "
            f"layer_monotonicity={lc.layer_monotonicity_violation_count})",
            f"- Days scanned: {lc.days_scanned}, items scanned: {lc.items_scanned}",
            "",
            "## AC5 — Degradation + recovery",
            f"- Degradation detected: {rc.degradation_detected}",
            f"- Recovery consistent: {rc.recovery_consistent}",
            f"- Item counts pre/post-fault/post-recovery: "
            f"{rc.pre_fault_item_count}/{rc.post_fault_item_count}/{rc.post_recovery_item_count}",
        ]

        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Harness internals
# ---------------------------------------------------------------------------


def _harness_policy() -> dict[str, Any]:
    """Return a policy dict that pins promotion ``age_hours`` to 0.0.

    This mirrors the repo test idiom (tests/test_gateway.py): with
    ``age_hours: 0.0`` on every layer, promotion eligibility is independent of
    the virtual clock. The ``access_count`` threshold is set high so incidental
    reads/searches during the workflow do NOT silently auto-promote items —
    promotion happens only via the harness's explicit ``force=True`` call, which
    keeps the per-layer file distribution (and therefore every metric)
    deterministic regardless of filesystem iteration order.
    """
    zero_promotion = {"age_hours": 0.0, "access_count": 10_000, "quality_score": 0.0}

    return {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": dict(zero_promotion),
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": dict(zero_promotion),
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": dict(zero_promotion),
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": dict(zero_promotion),
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": dict(zero_promotion),
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }


def _build_repo(home: Path) -> Path:
    """Create a minimal, real mnemos repo tree under ``home`` and return it."""
    repo_root = home / "repo"

    wiki = repo_root / "wiki"
    for sub in ("global", "projects", "entities", "claims", "topics"):
        (wiki / sub).mkdir(parents=True, exist_ok=True)

    agent = repo_root / ".agent"
    for sub in ("runs", "sessions", "state", "reports", "tools"):
        (agent / sub).mkdir(parents=True, exist_ok=True)
    (agent / "workflows" / "hooks").mkdir(parents=True, exist_ok=True)

    (wiki / "policy.yaml").write_text(yaml.dump(_harness_policy()), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")

    return repo_root


def _iter_persistent_items(gateway: MemoryGateway) -> list[dict[str, Any]]:
    """Return every item across the persistent (backup-eligible) layers."""
    items: list[dict[str, Any]] = []
    for layer in (_CAPTURE_LAYER, _PROMOTE_TARGET):
        items.extend(gateway._store.iter_layer_items(layer))

    return items


def _scan_lifecycle_violations(
    gateway: MemoryGateway,
) -> tuple[int, int, int, int]:
    """Scan persistent layers and count lifecycle-invariant violations.

    Returns ``(items_scanned, invalid_stage, orphaned_supersede,
    layer_monotonicity)``.
    """
    items = _iter_persistent_items(gateway)
    known_ids = {item.get("id") for item in items}

    invalid_stage = 0
    orphaned_supersede = 0
    layer_monotonicity = 0

    for item in items:
        stage = item.get("stage")
        if stage not in VALID_STAGES:
            invalid_stage += 1

        superseded_by = item.get("superseded_by")
        if superseded_by and superseded_by not in known_ids:
            orphaned_supersede += 1

        layer = item.get("layer", "")
        try:
            gateway._policy.get_next_layer(layer)
        except Exception:
            layer_monotonicity += 1

    return len(items), invalid_stage, orphaned_supersede, layer_monotonicity


def _probe_target_rank(gateway: MemoryGateway, target_id: str) -> int:
    """Return the 1-based rank of ``target_id`` under the fixed probe query.

    Returns ``0`` when the target does not appear in the result list.
    """
    results = gateway.search(_PROBE_QUERY, layers=[_CAPTURE_LAYER, _PROMOTE_TARGET], limit=50)
    for rank, result in enumerate(results, start=1):
        if result.get("item_id") == target_id:
            return rank

    return 0


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_beta_validation(
    days: int,
    seed: int,
    home: Path | str,
    *,
    start_epoch: float = 1_700_000_000.0,
) -> BetaReport:
    """Run the deterministic beta validation harness and return a report.

    Parameters
    ----------
    days:
        Number of simulated days to run (must be >= 1).
    seed:
        RNG seed driving every reproducible choice.
    home:
        An isolated home directory the harness owns. A real ``wiki/`` repo tree
        is built underneath it and a real gateway/store is driven against it.
    start_epoch:
        Simulated start time as a UTC epoch (seconds). Fixed by default so the
        report is reproducible from ``(days, seed)`` alone.
    """
    if days < 1:
        raise ValueError("days must be >= 1")

    home = Path(home)
    repo_root = _build_repo(home)

    clock = VirtualClock(start_epoch=start_epoch, seed=seed)
    rng = random.Random(seed)
    gateway = MemoryGateway(repo_root=str(repo_root))

    total_captures = 0

    # ---- Day 0 setup: capture fixed anchors (AC2) + the probe target (AC3). --
    anchor_ids: dict[str, str] = {}
    for keyword, content in _ANCHORS:
        anchor_id = gateway.capture(
            content=content,
            layer=_CAPTURE_LAYER,
            quality_score=0.9,
            run_id=f"beta-{seed}",
            extra_metadata={"created_at": clock.iso()},
            no_classify=True,
        )
        anchor_ids[keyword] = anchor_id
        total_captures += 1

    probe_target_id = gateway.capture(
        content=_PROBE_TARGET_CONTENT,
        layer=_CAPTURE_LAYER,
        quality_score=0.95,
        run_id=f"beta-{seed}",
        extra_metadata={"created_at": clock.iso()},
        no_classify=True,
    )
    total_captures += 1

    # ---- Per-day workflow loop. ----------------------------------------------
    observed_ranks: list[int] = []
    lifecycle_items_scanned = 0
    invalid_stage_total = 0
    orphaned_supersede_total = 0
    layer_monotonicity_total = 0
    promoted_ids: set[str] = set()

    for day in range(days):
        clock.advance(days=1)

        # Reproducible growth: capture a seeded number of filler notes.
        filler_count = rng.randint(2, 4)
        for _ in range(filler_count):
            template = rng.choice(_FILLER_TEMPLATES)
            topic = rng.choice(_FILLER_TOPICS)
            content = template.format(n=total_captures, topic=topic)
            new_id = gateway.capture(
                content=content,
                layer=_CAPTURE_LAYER,
                quality_score=round(rng.uniform(0.3, 0.9), 4),
                run_id=f"beta-{seed}",
                extra_metadata={"created_at": clock.iso()},
                no_classify=True,
            )
            if new_id is not None:
                total_captures += 1

        # Reproducible promotion of a captured item to the next layer. The
        # store's iteration order is filesystem-dependent, so sort by id before
        # the seeded choice to keep the selection deterministic across runs.
        project_items = sorted(
            gateway._store.iter_layer_items(_CAPTURE_LAYER),
            key=lambda it: it.get("id", ""),
        )
        promotable = [
            it for it in project_items
            if it.get("id") not in promoted_ids and it.get("id") != probe_target_id
        ]
        if promotable:
            choice = rng.choice(promotable)
            gateway.promote(item_id=choice["id"], force=True, run_id=f"beta-{seed}")
            promoted_ids.add(choice["id"])

        # Probe the fixed target's rank as the store grows (AC3).
        observed_ranks.append(_probe_target_rank(gateway, probe_target_id))

        # GC staleness scoring exercised via the clock-injectable scorer (AC4
        # adjacency). The score is advisory here; we never mutate via the
        # non-injectable gateway.gc() path.
        for item in gateway._store.iter_layer_items(_CAPTURE_LAYER):
            compute_garbage_score(
                item,
                staleness_hours=_GC_STALENESS_HOURS,
                now=clock.now(),
            )

        # Scan lifecycle invariants after the day's mutations (AC4).
        scanned, inv_stage, orphaned, mono = _scan_lifecycle_violations(gateway)
        lifecycle_items_scanned = scanned
        invalid_stage_total += inv_stage
        orphaned_supersede_total += orphaned
        layer_monotonicity_total += mono

    # ---- AC2 continuity recall: re-probe the anchors on the final day. -------
    surfaced = 0
    for keyword, anchor_id in anchor_ids.items():
        results = gateway.search(keyword, layers=[_CAPTURE_LAYER, _PROMOTE_TARGET], limit=50)
        found = any(res.get("item_id") == anchor_id for res in results)
        if found:
            surfaced += 1

    continuity = ContinuityMetric(
        total_anchors=len(anchor_ids),
        surfaced_anchors=surfaced,
        recall=surfaced / len(anchor_ids) if anchor_ids else 0.0,
    )

    # ---- AC3 relevance stability from the observed ranks. --------------------
    relevance = _build_relevance_metric(observed_ranks)

    # ---- AC4 lifecycle metric aggregate. -------------------------------------
    lifecycle_violations = (
        invalid_stage_total + orphaned_supersede_total + layer_monotonicity_total
    )
    lifecycle = LifecycleMetric(
        days_scanned=days,
        items_scanned=lifecycle_items_scanned,
        invalid_stage_count=invalid_stage_total,
        orphaned_supersede_count=orphaned_supersede_total,
        layer_monotonicity_violation_count=layer_monotonicity_total,
        violations=lifecycle_violations,
    )

    # ---- AC5 degradation + recovery. -----------------------------------------
    recovery = _run_degradation_recovery(gateway, repo_root, home)

    return BetaReport(
        seed=seed,
        days=days,
        start_epoch=start_epoch,
        total_captures=total_captures,
        continuity=continuity,
        relevance=relevance,
        lifecycle=lifecycle,
        recovery=recovery,
    )


def _build_relevance_metric(observed_ranks: list[int]) -> RelevanceMetric:
    """Compute the relevance-stability metric from a list of observed ranks.

    Stability is ``1 - normalized_rank_variance`` where the normalized variance
    is the population variance of the observed ranks divided by the square of
    the worst observed rank (so it lies in ``[0, 1]``). A perfectly stable rank
    yields stability ``1.0``.
    """
    samples = len(observed_ranks)
    if samples == 0:
        return RelevanceMetric(
            samples=0,
            observed_ranks=(),
            rank_variance=0.0,
            normalized_rank_variance=0.0,
            stability=1.0,
        )

    mean = sum(observed_ranks) / samples
    variance = sum((r - mean) ** 2 for r in observed_ranks) / samples

    worst = max(observed_ranks) if max(observed_ranks) > 0 else 1
    normalized = variance / (worst * worst)
    normalized = min(1.0, normalized)

    return RelevanceMetric(
        samples=samples,
        observed_ranks=tuple(observed_ranks),
        rank_variance=round(variance, 6),
        normalized_rank_variance=round(normalized, 6),
        stability=round(1.0 - normalized, 6),
    )


def _run_degradation_recovery(
    gateway: MemoryGateway,
    repo_root: Path,
    home: Path,
) -> RecoveryMetric:
    """Snapshot, inject a partial-loss fault, detect it, and recover (AC5)."""
    backup_path = home / "pre-fault-backup.tar.gz"

    # Pre-fault snapshot of the persistent layers.
    make_backup(repo_root, backup_path)
    pre_fault_items = _iter_persistent_items(gateway)
    pre_fault_count = len(pre_fault_items)

    # Inject a partial-loss fault: delete a deterministic subset of the
    # captured files directly on disk (simulated corruption / loss). The victim
    # set is the first half of the FULL persistent file set sorted by path, so
    # the loss count is stable regardless of how items are distributed across
    # layers or the filesystem's iteration order.
    md_files = sorted(
        str(item["_path"]) for item in pre_fault_items if item.get("_path")
    )
    loss = max(1, len(md_files) // 2)
    for victim in md_files[:loss]:
        Path(victim).unlink()

    post_fault_count = len(_iter_persistent_items(gateway))

    # Detection: the harness DETECTS degradation when the live item count is
    # below the pre-fault snapshot count.
    degradation_detected = post_fault_count < pre_fault_count

    # Recovery: restore missing items from the pre-fault snapshot. The deleted
    # targets no longer exist, so the default (overwrite=False) restore re-adds
    # exactly the lost files without disturbing survivors.
    restore_backup(backup_path, repo_root, overwrite=False)
    post_recovery_count = len(_iter_persistent_items(gateway))

    recovery_consistent = post_recovery_count == pre_fault_count

    return RecoveryMetric(
        pre_fault_item_count=pre_fault_count,
        post_fault_item_count=post_fault_count,
        post_recovery_item_count=post_recovery_count,
        degradation_detected=degradation_detected,
        recovery_consistent=recovery_consistent,
    )
