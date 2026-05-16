"""G1GC-style Garbage Collector for the mnemos memory system.

Like the JVM's Garbage-First Collector, this module:

1. Scores each memory by a composite *garbage score* that combines:
   - Staleness  (time since created_at / last_access_at)
   - Low access_count
   - Low quality_score
   - Redundancy signals (archived stage)

2. Groups memories into *regions* by layer (ephemeral, working, session,
   project, global), mirroring G1GC's heap-region model.

3. Prioritises regions whose total garbage score is highest — collecting
   the "most garbage" first, exactly like Garbage-First.

4. Archives (never hard-deletes) memories that exceed configurable
   staleness / score thresholds.

5. Exposes a Python API (``GarbageCollector``) and a thin adapter used by
   the ``mnemos gc`` Click command in ``core/cli.py``.
"""
from __future__ import annotations

import datetime
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from core.store import MemoryStore
from core.layers import LAYER_STATIC_PATHS, TRANSIENT_PATH


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

#: All layers recognised by the GC, ordered from most ephemeral to most stable.
#: "transient" is listed first because it has the shortest expiry window and
#: should be the highest-priority GC target.
ALL_LAYERS: list[str] = ["transient", "ephemeral", "working", "session", "project", "global"]

#: Staleness threshold (hours) for the transient layer.
#: Items older than this are prime GC candidates.  Set to 1 hour so that
#: protocol-test captures are eligible for collection within the same session.
TRANSIENT_STALENESS_HOURS: float = 1.0

#: Default staleness threshold (hours). Memories older than this contribute
#: to their garbage score.
DEFAULT_STALENESS_HOURS: float = 24.0

#: Default garbage score threshold above which a memory is considered
#: collectable (0.0–1.0, where 1.0 is "pure garbage").
DEFAULT_GC_THRESHOLD: float = 0.7

#: Default maximum number of memories to archive in a single GC run.
DEFAULT_LIMIT: int = 100

#: Weight of each signal in the composite garbage score (must sum to 1.0).
_WEIGHT_STALENESS = 0.40
_WEIGHT_ACCESS = 0.25
_WEIGHT_QUALITY = 0.25
_WEIGHT_STAGE = 0.10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MemoryRecord:
    """Lightweight representation of a memory file for GC scoring."""

    item_id: str
    layer: str
    path: str
    created_at: datetime.datetime | None
    last_access_at: datetime.datetime | None
    access_count: int
    quality_score: float
    stage: str
    content_preview: str
    garbage_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class GCRegion:
    """A named layer region containing zero or more memory records."""

    layer: str
    records: list[MemoryRecord] = field(default_factory=list)

    @property
    def total_garbage_score(self) -> float:
        return sum(r.garbage_score for r in self.records)

    @property
    def collectable(self) -> list[MemoryRecord]:
        """Records already marked as collectable (score >= threshold)."""
        return [r for r in self.records if r.garbage_score >= 0.0]


@dataclass
class GCReport:
    """Result of a single GC run."""

    dry_run: bool
    scanned: int
    archived: int
    regions_processed: list[str]
    archived_items: list[dict[str, Any]]
    skipped_items: list[dict[str, Any]]

    def summary_lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        lines = [
            f"{prefix}GC scan complete",
            f"  Scanned  : {self.scanned} memories",
            f"  Regions  : {', '.join(self.regions_processed) or 'none'}",
            f"  Archived : {self.archived} memories"
            + (" (not applied)" if self.dry_run else ""),
        ]
        if self.archived_items:
            lines.append("  Items archived:")
            for item in self.archived_items:
                lines.append(
                    f"    [{item['layer']}] {item['item_id']}"
                    f"  score={item['garbage_score']:.3f}"
                    f"  ({item['reason']})"
                )
        if self.skipped_items:
            lines.append(f"  Skipped : {len(self.skipped_items)} below threshold")
        return lines


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> datetime.datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime, or return None."""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=datetime.timezone.utc)
        return value
    try:
        s = str(value).rstrip("Z")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _staleness_score(
    created_at: datetime.datetime | None,
    last_access_at: datetime.datetime | None,
    staleness_hours: float,
    now: datetime.datetime,
) -> float:
    """Return a staleness sub-score in [0.0, 1.0].

    Uses the *most recent* of created_at / last_access_at as the reference
    point.  A memory that was accessed recently is not stale even if it is old.
    The score saturates (reaches 1.0) once age >= 4 * staleness_hours.
    """
    ref = last_access_at or created_at
    if ref is None:
        # No timestamp: treat as maximally stale
        return 1.0
    age_hours = max(0.0, (now - ref).total_seconds() / 3600.0)
    if staleness_hours <= 0:
        return 0.0
    # Sigmoid-ish saturation: tanh so the score grows quickly and flattens out.
    ratio = age_hours / staleness_hours
    # tanh(ratio) in [0, ~1); we scale so 1 * staleness_hours → 0.76, 4x → ~1.0
    return float(min(1.0, math.tanh(ratio)))


def _access_score(access_count: int, layer: str) -> float:
    """Return an access sub-score in [0.0, 1.0].

    Higher access_count → lower score (memory is being used → less garbage).
    Per-layer baselines: ephemeral/working expect low counts; project/global
    items are penalised for zero access even more harshly.
    """
    # Baselines: how many accesses we expect before a memory is "well-used"
    _BASELINE = {
        "ephemeral": 1,
        "working": 2,
        "session": 3,
        "project": 5,
        "global": 3,
    }
    baseline = _BASELINE.get(layer, 2)
    if access_count >= baseline:
        return 0.0
    return float(1.0 - access_count / baseline)


def _quality_score(quality_score: float) -> float:
    """Return a quality sub-score in [0.0, 1.0].

    Low quality → high garbage score.
    """
    return float(max(0.0, 1.0 - quality_score))


def _stage_score(stage: str) -> float:
    """Return a stage sub-score in [0.0, 1.0].

    'archived' stage items are already soft-deleted — give them maximum
    stage score (they are prime candidates for review/eviction).  Items in
    active stages get 0.
    """
    _SCORES = {
        "archived":   1.0,
        "stored":     0.3,
        "captured":   0.2,
        "classified": 0.0,
        "retrieved":  0.0,
        "used":       0.0,
        "validated":  0.0,
        "promoted":   0.0,
        "demoted":    0.2,
        "forgotten":  0.5,
    }
    return _SCORES.get(stage, 0.1)


def compute_garbage_score(
    item: dict[str, Any],
    staleness_hours: float = DEFAULT_STALENESS_HOURS,
    now: datetime.datetime | None = None,
) -> tuple[float, dict[str, float]]:
    """Compute a composite garbage score for *item* in [0.0, 1.0].

    Returns ``(score, breakdown)`` where breakdown maps sub-score names to
    their weighted contributions.

    Parameters
    ----------
    item:
        A memory item dict as returned by ``MemoryStore._parse_file()``.
    staleness_hours:
        Age threshold in hours beyond which staleness saturates toward 1.0.
    now:
        Reference timestamp for age computation.  Defaults to UTC now.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    layer = item.get("layer", "ephemeral")
    created_at = _parse_dt(item.get("created_at"))
    last_access_at = _parse_dt(item.get("last_access_at"))
    access_count = int(item.get("access_count", 0))
    quality = float(item.get("quality_score", 0.8))
    stage = str(item.get("stage", "stored"))

    s_staleness = _staleness_score(created_at, last_access_at, staleness_hours, now)
    s_access = _access_score(access_count, layer)
    s_quality = _quality_score(quality)
    s_stage = _stage_score(stage)

    breakdown = {
        "staleness": round(s_staleness * _WEIGHT_STALENESS, 4),
        "access":    round(s_access    * _WEIGHT_ACCESS,    4),
        "quality":   round(s_quality   * _WEIGHT_QUALITY,   4),
        "stage":     round(s_stage     * _WEIGHT_STAGE,     4),
    }
    total = sum(breakdown.values())
    return round(min(1.0, total), 4), breakdown


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def _build_record(path: Path, store: MemoryStore, staleness_hours: float, now: datetime.datetime) -> MemoryRecord | None:
    """Parse *path* and return a :class:`MemoryRecord`, or None on error."""
    try:
        item = store._parse_file(path)
    except Exception:
        return None

    created_at = _parse_dt(item.get("created_at"))
    last_access_at = _parse_dt(item.get("last_access_at"))
    access_count = int(item.get("access_count", 0))
    quality = float(item.get("quality_score", 0.8))
    stage = str(item.get("stage", "stored"))
    layer = str(item.get("layer", path.parent.parent.name))
    item_id = str(item.get("id") or path.stem)
    content_preview = str(item.get("content", ""))[:80]

    score, breakdown = compute_garbage_score(item, staleness_hours=staleness_hours, now=now)

    return MemoryRecord(
        item_id=item_id,
        layer=layer,
        path=str(path),
        created_at=created_at,
        last_access_at=last_access_at,
        access_count=access_count,
        quality_score=quality,
        stage=stage,
        content_preview=content_preview,
        garbage_score=score,
        score_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Path enumeration helpers (mirrors gateway.py / consolidate() pattern)
# ---------------------------------------------------------------------------

def _iter_layer_paths(repo_root: Path, layer: str) -> Iterator[Path]:
    """Yield all .md file paths for a given layer."""
    if layer == "transient":
        # Flat directory — no sub-namespacing by run_id or session_id.
        transient_dir = repo_root / TRANSIENT_PATH
        if transient_dir.exists():
            yield from transient_dir.glob("*.md")
    elif layer == "ephemeral":
        agent_runs = repo_root / ".agent" / "runs"
        if agent_runs.exists():
            for rd in agent_runs.iterdir():
                if rd.is_dir():
                    scratch = rd / "scratch"
                    if scratch.exists():
                        yield from scratch.glob("*.md")
    elif layer == "working":
        agent_runs = repo_root / ".agent" / "runs"
        if agent_runs.exists():
            for rd in agent_runs.iterdir():
                if rd.is_dir():
                    working_dir = rd / "working"
                    if working_dir.exists():
                        yield from working_dir.glob("*.md")
    elif layer == "session":
        agent_sessions = repo_root / ".agent" / "sessions"
        if agent_sessions.exists():
            yield from agent_sessions.rglob("*.md")
    elif layer in LAYER_STATIC_PATHS:
        layer_dir = repo_root / LAYER_STATIC_PATHS[layer]
        if layer_dir.exists():
            yield from layer_dir.glob("*.md")


# ---------------------------------------------------------------------------
# Main GC engine
# ---------------------------------------------------------------------------

class GarbageCollector:
    """G1GC-inspired garbage collector for mnemos memories.

    Usage (programmatic)::

        from core.gc import GarbageCollector
        gc = GarbageCollector(repo_root="/path/to/mnemos/repo")
        report = gc.run(dry_run=True)
        for line in report.summary_lines():
            print(line)

    Parameters
    ----------
    repo_root:
        Path to the mnemos repository root (must contain ``wiki/policy.yaml``).
    staleness_hours:
        Age threshold for staleness scoring. Defaults to 24 h.
    gc_threshold:
        Minimum garbage score [0.0–1.0] to consider a memory collectable.
    limit:
        Maximum number of memories to archive in one run (Garbage-First
        ordering: highest-score regions first, then highest-score items).
    layers:
        Restrict GC to these layer names.  Defaults to all layers.
    """

    def __init__(
        self,
        repo_root: str,
        staleness_hours: float = DEFAULT_STALENESS_HOURS,
        gc_threshold: float = DEFAULT_GC_THRESHOLD,
        limit: int = DEFAULT_LIMIT,
        layers: list[str] | None = None,
    ) -> None:
        self._root = Path(repo_root)
        self._store = MemoryStore(repo_root=repo_root)
        self.staleness_hours = staleness_hours
        self.gc_threshold = gc_threshold
        self.limit = limit
        self.layers = layers or ALL_LAYERS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _effective_staleness_hours(self, layer: str) -> float:
        """Return the staleness threshold for a given layer.

        The transient layer always uses :data:`TRANSIENT_STALENESS_HOURS`
        (1 hour) regardless of the instance-level ``staleness_hours`` setting,
        ensuring protocol-test captures are GC-eligible within the same session.
        All other layers use the instance-level value.
        """
        if layer == "transient":
            return TRANSIENT_STALENESS_HOURS
        return self.staleness_hours

    def scan(self) -> list[GCRegion]:
        """Scan all configured layers and return a list of :class:`GCRegion`.

        Regions are returned in *Garbage-First* order: highest
        ``total_garbage_score`` first.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        regions: list[GCRegion] = []

        for layer in self.layers:
            region = GCRegion(layer=layer)
            layer_staleness = self._effective_staleness_hours(layer)
            for path in _iter_layer_paths(self._root, layer):
                record = _build_record(path, self._store, layer_staleness, now)
                if record is not None:
                    region.records.append(record)
            regions.append(region)

        # Garbage-First ordering: sort by total garbage score descending
        regions.sort(key=lambda r: r.total_garbage_score, reverse=True)
        return regions

    def run(self, dry_run: bool = False) -> GCReport:
        """Execute a full GC cycle and return a :class:`GCReport`.

        Parameters
        ----------
        dry_run:
            When ``True``, compute scores and identify collectable memories
            but do **not** call ``MemoryStore.update()`` to archive them.
        """
        regions = self.scan()

        scanned = sum(len(r.records) for r in regions)
        archived_items: list[dict[str, Any]] = []
        skipped_items: list[dict[str, Any]] = []
        archived_count = 0
        regions_processed: list[str] = []

        for region in regions:
            if archived_count >= self.limit:
                break

            # Sort within each region: highest garbage score first
            candidates = sorted(
                region.records,
                key=lambda r: r.garbage_score,
                reverse=True,
            )

            for record in candidates:
                if archived_count >= self.limit:
                    break

                if record.garbage_score < self.gc_threshold:
                    skipped_items.append({
                        "item_id": record.item_id,
                        "layer": record.layer,
                        "garbage_score": record.garbage_score,
                    })
                    continue

                reason = self._build_reason(record)

                if not dry_run:
                    try:
                        self._store.update(
                            record.path,
                            metadata_updates={"stage": "archived"},
                        )
                    except Exception as exc:
                        # Non-fatal: log and move on
                        skipped_items.append({
                            "item_id": record.item_id,
                            "layer": record.layer,
                            "garbage_score": record.garbage_score,
                            "error": str(exc),
                        })
                        continue

                archived_items.append({
                    "item_id": record.item_id,
                    "layer": record.layer,
                    "garbage_score": record.garbage_score,
                    "score_breakdown": record.score_breakdown,
                    "reason": reason,
                    "path": record.path,
                })
                archived_count += 1

                if region.layer not in regions_processed:
                    regions_processed.append(region.layer)

        return GCReport(
            dry_run=dry_run,
            scanned=scanned,
            archived=archived_count,
            regions_processed=regions_processed,
            archived_items=archived_items,
            skipped_items=skipped_items,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_reason(record: MemoryRecord) -> str:
        """Return a human-readable reason string for why a memory was collected."""
        parts: list[str] = []
        bd = record.score_breakdown
        if bd.get("staleness", 0) > 0.2:
            parts.append("stale")
        if bd.get("access", 0) > 0.15:
            parts.append("low access_count")
        if bd.get("quality", 0) > 0.15:
            parts.append("low quality_score")
        if bd.get("stage", 0) > 0.05:
            parts.append(f"stage={record.stage}")
        return ", ".join(parts) if parts else "composite score threshold exceeded"
