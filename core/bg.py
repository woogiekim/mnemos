"""Background Operations Engine for mnemos.

Provides a lightweight, hook-driven background maintenance system that runs
automatically via Claude Code's PostToolUse hook — no separate daemon required.

Inspired by the JVM's G1GC model:  background threads run concurrently and
unobtrusively, keeping the heap (memory store) healthy without blocking the
foreground workload (Claude's active session).

Design principles
-----------------
1. **Throttling by default** — a /tmp timestamp file prevents the check from
   running more than once per configurable interval (default: 5 minutes).
   PostToolUse fires after every tool call; without throttling this would be
   prohibitively expensive.

2. **Silent unless significant** — the check emits output only when it
   actually archives, promotes, or detects duplicates. Silent runs produce
   no stdout so Claude's context is not polluted.

3. **No new dependencies** — everything is built on the existing GC, gateway,
   and store infrastructure.  No new pip packages required.

4. **Cheap duplicate detection** — uses content-hash equality (SHA-256 of
   normalised content) across same-layer items. This is O(n) and well under
   the latency budget for a throttled background check.

Public API
----------
- ``should_run(interval_minutes)`` — True if enough time has passed
- ``touch_timestamp()`` — record that a run just happened
- ``run_gc(repo_root, **kwargs)`` — thin wrapper around GarbageCollector
- ``run_auto_promote(repo_root)`` — call gateway.consolidate()
- ``find_duplicates(repo_root, layers, similarity_threshold)`` — hash-based dedup
- ``BackgroundCheckResult`` — dataclass summarising a full check run
- ``run_background_check(repo_root, ...)`` — orchestrates all operations and
  returns a BackgroundCheckResult
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.gc import (
    GarbageCollector,
    GCReport,
    DEFAULT_STALENESS_HOURS,
    DEFAULT_GC_THRESHOLD,
    DEFAULT_LIMIT,
)
from core.store import MemoryStore


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default throttle interval (minutes). PostToolUse fires after every tool;
#: we want at most one background check per this many minutes.
DEFAULT_INTERVAL_MINUTES: int = 5

#: Layer ordering used when deciding which direction "promotion" goes.
LAYER_ORDER: list[str] = ["ephemeral", "working", "session", "project", "global"]

#: Layers eligible for auto-promotion by the background check.
#: global has no higher layer — exclude it.
PROMOTABLE_LAYERS: list[str] = ["ephemeral", "working", "session", "project"]

#: Layers eligible for GC (all of them).
GC_LAYERS: list[str] = ["ephemeral", "working", "session", "project", "global"]

#: Layers to scan for duplicate detection (expensive at scale; default to the
#: volatile ones where duplicates accumulate fastest).
DEFAULT_DEDUP_LAYERS: list[str] = ["session", "project", "global"]


# ---------------------------------------------------------------------------
# Timestamp-based throttle
# ---------------------------------------------------------------------------

def _timestamp_path() -> Path:
    """Return the path to the background-check timestamp file.

    Uses /tmp directly so the path matches the bash PostToolUse hook which
    also writes to /tmp/mnemos-bg-check-{uid}.ts.  On macOS tempfile.gettempdir()
    returns a session-specific /var/folders/… path that differs from /tmp,
    breaking the shared throttle.
    """
    uid = os.getuid() if hasattr(os, "getuid") else "0"
    return Path("/tmp") / f"mnemos-bg-check-{uid}.ts"


def should_run(interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> bool:
    """Return True if the background check should run now.

    Checks whether the timestamp file either does not exist or is older than
    *interval_minutes* minutes.  This is the single gate that prevents the
    hook from being a performance bottleneck.

    Parameters
    ----------
    interval_minutes:
        Minimum gap between successive background checks.
    """
    ts_path = _timestamp_path()
    if not ts_path.exists():
        return True
    try:
        mtime = ts_path.stat().st_mtime
        elapsed = time.time() - mtime
        return elapsed >= interval_minutes * 60
    except OSError:
        return True


def touch_timestamp() -> None:
    """Record that a background check just ran (update the /tmp timestamp)."""
    ts_path = _timestamp_path()
    try:
        ts_path.touch()
    except OSError:
        pass  # /tmp not writable — skip silently


# ---------------------------------------------------------------------------
# GC operation
# ---------------------------------------------------------------------------

def run_gc(
    repo_root: str,
    *,
    staleness_hours: float = DEFAULT_STALENESS_HOURS,
    gc_threshold: float = DEFAULT_GC_THRESHOLD,
    limit: int = DEFAULT_LIMIT,
    layers: list[str] | None = None,
) -> GCReport:
    """Run G1GC-style garbage collection in the background.

    Parameters
    ----------
    repo_root:
        Path to the mnemos repository root.
    staleness_hours:
        Age threshold for staleness scoring.
    gc_threshold:
        Minimum garbage score [0.0–1.0] to collect a memory.
    limit:
        Maximum number of memories to archive in this run.  The background
        check uses a smaller default limit than the manual ``mnemos gc``
        command to bound latency.
    layers:
        Restrict GC to these layer names.  Defaults to all layers.

    Returns
    -------
    GCReport
        Detailed report including per-item scores and archived items.
    """
    collector = GarbageCollector(
        repo_root=repo_root,
        staleness_hours=staleness_hours,
        gc_threshold=gc_threshold,
        limit=limit,
        layers=layers or GC_LAYERS,
    )
    return collector.run(dry_run=False)


# ---------------------------------------------------------------------------
# Auto-promotion operation
# ---------------------------------------------------------------------------

def run_auto_promote(repo_root: str) -> int:
    """Sweep all memories and auto-promote eligible ones per policy.yaml.

    Delegates entirely to ``MemoryGateway.consolidate()`` which already
    implements the full promotion logic (age, access_count, quality_score
    thresholds).  Returns the count of memories promoted.

    Parameters
    ----------
    repo_root:
        Path to the mnemos repository root.
    """
    from core.gateway import MemoryGateway
    from core.adapters.claude import ClaudeCodeAdapter

    gw = MemoryGateway(repo_root=repo_root)
    # Wire in-process promotion events so they can be captured if needed.
    ClaudeCodeAdapter().subscribe_to_event_bus(gw.event_bus)
    return gw.consolidate()


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

@dataclass
class DuplicateGroup:
    """A set of memory items with identical (or near-identical) content."""

    content_hash: str
    """SHA-256 of the normalised content shared by all items in this group."""

    items: list[dict[str, Any]] = field(default_factory=list)
    """List of memory item dicts (item_id, layer, path, content_preview)."""

    @property
    def primary(self) -> dict[str, Any]:
        """The item to keep: the one in the highest layer (most promoted)."""
        def layer_rank(item: dict) -> int:
            try:
                return LAYER_ORDER.index(item.get("layer", "ephemeral"))
            except ValueError:
                return 0
        return max(self.items, key=layer_rank)

    @property
    def duplicates(self) -> list[dict[str, Any]]:
        """Items that are duplicates of the primary (i.e. all except primary)."""
        primary_id = self.primary.get("item_id")
        return [i for i in self.items if i.get("item_id") != primary_id]


def _normalise_content(content: str) -> str:
    """Normalise content for duplicate comparison.

    Applies Unicode NFKC normalisation first (collapses compatibility
    variants such as full-width letters and ligatures), then strips
    leading/trailing whitespace, collapses interior runs of whitespace to
    single spaces, and lower-cases the result.  This catches duplicates
    that differ only in formatting or Unicode representation.

    The NFKC step is load-bearing for the on-write dedup in
    ``gateway.capture()`` — the dedup key is ``(layer, hash(nfkc(content)))``
    so two payloads that differ only in Unicode compatibility form are
    treated as identical.
    """
    import unicodedata
    nfkc = unicodedata.normalize("NFKC", content)
    return re.sub(r"\s+", " ", nfkc.strip()).lower()


def _content_hash(content: str) -> str:
    """Return a SHA-256 hex digest of the *normalised* content."""
    normalised = _normalise_content(content)
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def find_duplicates(
    repo_root: str,
    layers: list[str] | None = None,
) -> list[DuplicateGroup]:
    """Scan specified layers and return groups of duplicate memories.

    Duplicate detection is hash-based (SHA-256 of normalised content), making
    it O(n) and safe to run in a throttled background context.  Items with
    identical normalised content are grouped; groups with only one item are
    discarded.

    Parameters
    ----------
    repo_root:
        Path to the mnemos repository root.
    layers:
        Layers to scan.  Defaults to ``DEFAULT_DEDUP_LAYERS``.

    Returns
    -------
    list[DuplicateGroup]
        Each group contains two or more items with the same content hash.
        Groups are returned in descending order by duplicate count.
    """
    scan_layers = layers or DEFAULT_DEDUP_LAYERS
    store = MemoryStore(repo_root=repo_root)

    hash_to_items: dict[str, list[dict[str, Any]]] = {}

    for layer in scan_layers:
        for item in store.iter_layer_items(layer):
            content = item.get("content", "")
            if not content.strip():
                continue
            h = _content_hash(content)
            raw_path = item.get("_path", "")
            record = {
                "item_id": str(item.get("id") or Path(raw_path).stem if raw_path else ""),
                "layer": str(item.get("layer", layer)),
                "path": raw_path,
                "content_preview": content[:80],
                "stage": str(item.get("stage", "stored")),
            }
            hash_to_items.setdefault(h, []).append(record)

    groups = [
        DuplicateGroup(content_hash=h, items=items)
        for h, items in hash_to_items.items()
        if len(items) >= 2
    ]
    groups.sort(key=lambda g: len(g.items), reverse=True)
    return groups


# ---------------------------------------------------------------------------
# Orchestrating result dataclass
# ---------------------------------------------------------------------------

@dataclass
class BackgroundCheckResult:
    """Aggregated result of a full background check run.

    Attributes
    ----------
    ran:
        True when the check actually executed (not throttled).
    gc_archived:
        Number of memories archived by GC.
    promoted:
        Number of memories auto-promoted.
    duplicate_groups:
        Duplicate groups detected (may be empty).
    messages:
        Human-readable summary lines emitted by the check.
    elapsed_ms:
        Wall-clock time of the check in milliseconds.
    """

    ran: bool = False
    gc_archived: int = 0
    promoted: int = 0
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    memory_os_enabled: bool = False
    memory_os_lifecycle_planned: int = 0
    memory_os_lifecycle_applied: int = 0
    memory_os_health_status: str | None = None
    memory_os_validation_passed: bool | None = None
    memory_os_repaired: int = 0
    memory_os_reindexed: int = 0
    memory_os_corrupt: int = 0
    memory_os_retrieval_status: str | None = None
    memory_os_evidence_paths: list[str] = field(default_factory=list)
    memory_os_errors: list[str] = field(default_factory=list)

    @property
    def has_activity(self) -> bool:
        """True when the check produced at least one notable event."""
        return bool(
            self.gc_archived > 0
            or self.promoted > 0
            or self.duplicate_groups
            or self.memory_os_lifecycle_applied > 0
            or self.memory_os_repaired > 0
            or self.memory_os_retrieval_status in {"degraded", "failed"}
            or self.memory_os_validation_passed is False
            or self.memory_os_errors
        )

    def to_context_block(self) -> str:
        """Render a ``<mnemos-context type="background-activity">`` block.

        Returns an empty string when :attr:`has_activity` is False (caller
        should not emit anything in that case).
        """
        if not self.has_activity:
            return ""

        lines: list[str] = ['<mnemos-context type="background-activity">']
        if self.gc_archived:
            lines.append(f"[mnemos bg] GC archived {self.gc_archived} stale/low-quality memories")
        if self.promoted:
            lines.append(f"[mnemos bg] Auto-promoted {self.promoted} memories")
        for group in self.duplicate_groups[:3]:  # cap at 3 to keep output short
            ids = ", ".join(i["item_id"] for i in group.duplicates[:2])
            primary_id = group.primary.get("item_id", "?")
            lines.append(
                f"[mnemos bg] Duplicate detected: {ids} duplicates {primary_id}"
                f" (keeping {primary_id} [{group.primary.get('layer')}])"
            )
        if len(self.duplicate_groups) > 3:
            lines.append(f"[mnemos bg] ... and {len(self.duplicate_groups) - 3} more duplicate group(s)")
        if self.memory_os_lifecycle_applied:
            lines.append(
                f"[mnemos bg] Memory OS applied {self.memory_os_lifecycle_applied} lifecycle transition(s)"
            )
        if self.memory_os_repaired:
            lines.append(
                f"[mnemos bg] Memory OS repaired {self.memory_os_repaired} metadata/index issue(s)"
            )
        if self.memory_os_retrieval_status in {"degraded", "failed"}:
            lines.append(f"[mnemos bg] Memory OS retrieval backends {self.memory_os_retrieval_status}")
        if self.memory_os_validation_passed is False:
            lines.append("[mnemos bg] Memory OS health validation failed")
        for error in self.memory_os_errors[:2]:
            lines.append(f"[mnemos bg] Memory OS maintenance error: {error}")
        lines.append("</mnemos-context>")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run_background_check(
    repo_root: str,
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    gc_enabled: bool = True,
    gc_staleness_hours: float = DEFAULT_STALENESS_HOURS,
    gc_threshold: float = DEFAULT_GC_THRESHOLD,
    gc_limit: int = 20,
    auto_promote_enabled: bool = True,
    dedup_enabled: bool = True,
    dedup_layers: list[str] | None = None,
    memory_os_enabled: bool = False,
    memory_os_recover: bool = False,
    memory_os_apply: bool = False,
    memory_os_min_score: float | None = None,
    memory_os_layers: list[str] | None = None,
    memory_os_record: bool = True,
    force: bool = False,
) -> BackgroundCheckResult:
    """Run all background maintenance operations.

    This is the function called by the ``mnemos bg-check`` CLI command,
    which in turn is invoked by the PostToolUse hook.

    The check is a no-op (``BackgroundCheckResult(ran=False)``) when
    :func:`should_run` returns False, unless *force* is True.

    Parameters
    ----------
    repo_root:
        Path to the mnemos repository root.
    interval_minutes:
        Throttle: minimum gap between successive background checks.
    gc_enabled:
        Run GC phase.
    gc_staleness_hours:
        GC staleness threshold.
    gc_threshold:
        GC score threshold.
    gc_limit:
        Maximum memories to GC per background check (intentionally low to
        bound latency; manual ``mnemos gc`` uses a higher default).
    auto_promote_enabled:
        Run auto-promotion (consolidate) phase.
    dedup_enabled:
        Run duplicate detection phase.
    dedup_layers:
        Layers to scan for duplicates. Defaults to session, project, global.
    memory_os_enabled:
        Run opt-in Memory OS lifecycle reporting, metrics snapshot, and health validation.
    memory_os_recover:
        Repair recoverable metadata and reindex before scoring.
    memory_os_apply:
        Apply lifecycle transitions during the Memory OS phase. Defaults to dry-run reporting.
    memory_os_min_score:
        Optional uniform health validation threshold.
    memory_os_layers:
        Optional Memory OS layer filter.
    memory_os_record:
        Persist Memory OS lifecycle, metrics, and validation evidence.
    force:
        Skip the throttle check and always run.

    Returns
    -------
    BackgroundCheckResult
    """
    result = BackgroundCheckResult()

    if not force and not should_run(interval_minutes):
        return result

    t_start = time.monotonic()

    # Record run time immediately so concurrent runs don't pile up
    touch_timestamp()
    result.ran = True

    messages: list[str] = []

    # Phase 1: GC
    if gc_enabled:
        try:
            gc_report = run_gc(
                repo_root,
                staleness_hours=gc_staleness_hours,
                gc_threshold=gc_threshold,
                limit=gc_limit,
            )
            result.gc_archived = gc_report.archived
            if gc_report.archived:
                messages.append(
                    f"GC: archived {gc_report.archived} memories "
                    f"(regions: {', '.join(gc_report.regions_processed)})"
                )
        except Exception:
            pass  # GC failure is non-fatal in background context

    # Phase 2: Auto-promotion
    if auto_promote_enabled:
        try:
            promoted = run_auto_promote(repo_root)
            result.promoted = promoted
            if promoted:
                messages.append(f"Auto-promote: promoted {promoted} memories")
        except Exception:
            pass  # Promotion failure is non-fatal

    # Phase 3: Duplicate detection
    if dedup_enabled:
        try:
            groups = find_duplicates(repo_root, layers=dedup_layers)
            result.duplicate_groups = groups
            if groups:
                total_dupes = sum(len(g.duplicates) for g in groups)
                messages.append(
                    f"Duplicates: {len(groups)} group(s), {total_dupes} redundant memories"
                )
        except Exception:
            pass  # Dedup failure is non-fatal

    # Phase 4: opt-in Memory OS operational evidence and validation
    if memory_os_enabled:
        result.memory_os_enabled = True
        try:
            from core.gateway import MemoryGateway
            from core.operations import MemoryOperationsEngine

            gw = MemoryGateway(repo_root=repo_root)
            engine = MemoryOperationsEngine(gw)
            backend_report = engine.retrieval_backend_health()
            result.memory_os_retrieval_status = backend_report.status
            if memory_os_record:
                result.memory_os_evidence_paths.append(
                    str(engine.record_backend_health_report(backend_report, label="bg-check"))
                )
            if backend_report.partial_failure:
                messages.append(f"Memory OS: retrieval backends {backend_report.status}")

            if memory_os_recover:
                recovery_report = engine.recover_store(
                    dry_run=False,
                    layers=memory_os_layers,
                    reindex=True,
                )
                result.memory_os_repaired = recovery_report.repaired_count
                result.memory_os_reindexed = recovery_report.reindexed_count
                result.memory_os_corrupt = recovery_report.corrupt_count
                if memory_os_record:
                    result.memory_os_evidence_paths.append(
                        str(engine.record_recovery_report(recovery_report, label="bg-check"))
                    )
                if recovery_report.repaired_count:
                    messages.append(
                        f"Memory OS: repaired {recovery_report.repaired_count} metadata/index issue(s)"
                    )

            lifecycle_report = engine.run_lifecycle(
                dry_run=not memory_os_apply,
                layers=memory_os_layers,
            )
            result.memory_os_lifecycle_planned = lifecycle_report.planned_count
            result.memory_os_lifecycle_applied = lifecycle_report.applied_count

            if memory_os_record:
                result.memory_os_evidence_paths.append(
                    str(engine.record_lifecycle_report(lifecycle_report, label="bg-check"))
                )

            metrics = engine.compute_metrics(layers=memory_os_layers)
            if memory_os_record:
                result.memory_os_evidence_paths.append(
                    str(engine.record_metrics_snapshot(metrics, label="bg-check"))
                )

            validation = engine.validate_health(
                layers=memory_os_layers,
                metrics=metrics,
                min_score=memory_os_min_score,
            )
            result.memory_os_health_status = validation.status
            result.memory_os_validation_passed = validation.passed
            if memory_os_record:
                result.memory_os_evidence_paths.append(
                    str(engine.record_validation_report(validation, label="bg-check"))
                )

            if lifecycle_report.applied_count:
                messages.append(
                    f"Memory OS: applied {lifecycle_report.applied_count} lifecycle transition(s)"
                )
            if not validation.passed:
                messages.append("Memory OS: health validation failed")
        except Exception as exc:
            result.memory_os_errors.append(str(exc))
            messages.append("Memory OS: maintenance failed")

    t_end = time.monotonic()
    result.elapsed_ms = (t_end - t_start) * 1000
    result.messages = messages
    return result
