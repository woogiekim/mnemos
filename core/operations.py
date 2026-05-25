"""Operational memory execution, metrics, and recovery helpers."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import frontmatter

from core.contracts import TRUST_RANK, TrustLevel, normalize_trust_level
from core.layers import LAYER_STATIC_PATHS, TRANSIENT_PATH
from core.lifecycle import LifecycleAction, LifecycleDecision, MemoryLifecycleManager
from core.policy import VALID_STAGES


OPERATIONAL_LAYERS: tuple[str, ...] = (
    "transient",
    "ephemeral",
    "working",
    "session",
    "project",
    "global",
    "entities",
    "claims",
    "topics",
)


SCORE_NAMES: tuple[str, ...] = (
    "context_continuity_score",
    "retrieval_relevance_score",
    "historical_awareness_accuracy",
    "compression_preservation_quality",
    "lifecycle_consistency_rate",
    "persistent_memory_stability",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class LifecycleExecutionItem:
    """One planned or applied lifecycle transition."""

    item_id: str
    layer: str
    action: str
    reason: str
    target_layer: str | None = None
    target_stage: str | None = None
    applied: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "item_id": self.item_id,
            "layer": self.layer,
            "action": self.action,
            "reason": self.reason,
            "target_layer": self.target_layer,
            "target_stage": self.target_stage,
            "applied": self.applied,
            "error": self.error,
        }


@dataclass(frozen=True)
class LifecycleExecutionReport:
    """Summary of a lifecycle execution run."""

    status: str
    dry_run: bool
    evaluated_count: int
    retained_count: int
    planned_count: int
    applied_count: int
    failed_count: int
    items: tuple[LifecycleExecutionItem, ...]
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "evaluated_count": self.evaluated_count,
            "retained_count": self.retained_count,
            "planned_count": self.planned_count,
            "applied_count": self.applied_count,
            "failed_count": self.failed_count,
            "generated_at": self.generated_at,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class OperationalMetrics:
    """Memory OS operational health scores."""

    context_continuity_score: float
    retrieval_relevance_score: float
    historical_awareness_accuracy: float
    compression_preservation_quality: float
    lifecycle_consistency_rate: float
    persistent_memory_stability: float
    item_count: int
    layer_counts: dict[str, int]
    stage_counts: dict[str, int]
    issue_count: int
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "status": "ok",
            "generated_at": self.generated_at,
            "scores": {
                "context_continuity_score": self.context_continuity_score,
                "retrieval_relevance_score": self.retrieval_relevance_score,
                "historical_awareness_accuracy": self.historical_awareness_accuracy,
                "compression_preservation_quality": self.compression_preservation_quality,
                "lifecycle_consistency_rate": self.lifecycle_consistency_rate,
                "persistent_memory_stability": self.persistent_memory_stability,
            },
            "item_count": self.item_count,
            "layer_counts": dict(self.layer_counts),
            "stage_counts": dict(self.stage_counts),
            "issue_count": self.issue_count,
        }


@dataclass(frozen=True)
class OperationalHealthThresholds:
    """Calibration thresholds for Memory OS health validation."""

    context_continuity_score: float = 0.85
    retrieval_relevance_score: float = 0.85
    historical_awareness_accuracy: float = 0.85
    compression_preservation_quality: float = 0.85
    lifecycle_consistency_rate: float = 0.95
    persistent_memory_stability: float = 0.95

    @classmethod
    def uniform(cls, min_score: float) -> "OperationalHealthThresholds":
        """Use one threshold for every operational score."""
        score = _round_score(min_score)
        return cls(
            context_continuity_score=score,
            retrieval_relevance_score=score,
            historical_awareness_accuracy=score,
            compression_preservation_quality=score,
            lifecycle_consistency_rate=score,
            persistent_memory_stability=score,
        )

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serializable representation."""
        return {
            "context_continuity_score": self.context_continuity_score,
            "retrieval_relevance_score": self.retrieval_relevance_score,
            "historical_awareness_accuracy": self.historical_awareness_accuracy,
            "compression_preservation_quality": self.compression_preservation_quality,
            "lifecycle_consistency_rate": self.lifecycle_consistency_rate,
            "persistent_memory_stability": self.persistent_memory_stability,
        }


@dataclass(frozen=True)
class HealthGateResult:
    """One calibrated validation gate result."""

    name: str
    actual: float
    threshold: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "name": self.name,
            "actual": self.actual,
            "threshold": self.threshold,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class OperationalValidationReport:
    """Validation report for Memory OS operational health."""

    status: str
    passed: bool
    gates: tuple[HealthGateResult, ...]
    metrics: OperationalMetrics
    trend: dict[str, Any]
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "status": self.status,
            "passed": self.passed,
            "generated_at": self.generated_at,
            "gates": [gate.to_dict() for gate in self.gates],
            "metrics": self.metrics.to_dict(),
            "trend": self.trend,
        }


@dataclass(frozen=True)
class MemoryRecoveryIssue:
    """One recovery or reconciliation finding."""

    path: str
    code: str
    message: str
    item_id: str | None = None
    layer: str | None = None
    repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "path": self.path,
            "code": self.code,
            "message": self.message,
            "item_id": self.item_id,
            "layer": self.layer,
            "repaired": self.repaired,
        }


@dataclass(frozen=True)
class MemoryRecoveryReport:
    """Summary of store recovery and reconciliation."""

    status: str
    dry_run: bool
    scanned_count: int
    readable_count: int
    corrupt_count: int
    repaired_count: int
    reindexed_count: int
    issues: tuple[MemoryRecoveryIssue, ...]
    generated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "scanned_count": self.scanned_count,
            "readable_count": self.readable_count,
            "corrupt_count": self.corrupt_count,
            "repaired_count": self.repaired_count,
            "reindexed_count": self.reindexed_count,
            "generated_at": self.generated_at,
            "issues": [issue.to_dict() for issue in self.issues],
        }


class MemoryOperationsEngine:
    """Execute Memory OS operations through the existing gateway surface."""

    def __init__(
        self,
        gateway: Any,
        lifecycle_manager: MemoryLifecycleManager | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = gateway._store
        self._fts = gateway._fts
        self._root = Path(gateway._root)
        self._lifecycle = lifecycle_manager or MemoryLifecycleManager()
        self._evidence_dir = self._root / ".agent" / "reports" / "memory-os"

    def run_lifecycle(
        self,
        *,
        dry_run: bool = True,
        layers: list[str] | None = None,
        limit: int | None = None,
        include_retained: bool = False,
    ) -> LifecycleExecutionReport:
        """Plan or apply lifecycle transitions for operational memories."""
        items: list[LifecycleExecutionItem] = []
        evaluated_count = 0
        retained_count = 0
        planned_count = 0
        applied_count = 0
        failed_count = 0

        for item in self._iter_items(layers):
            if limit is not None and evaluated_count >= limit:
                break

            evaluated_count += 1
            item_id = _item_id(item)
            layer = str(item.get("layer") or _path_layer(item.get("_path")) or "unknown")
            decision = self._lifecycle.plan_transition(item)

            if decision.action is LifecycleAction.RETAIN:
                retained_count += 1
                if include_retained:
                    items.append(_execution_item(item_id, layer, decision, applied=False))
                continue

            planned_count += 1
            if dry_run:
                items.append(_execution_item(item_id, layer, decision, applied=False))
                continue

            try:
                self._apply_lifecycle_decision(item, decision)
            except Exception as exc:
                failed_count += 1
                items.append(
                    _execution_item(
                        item_id,
                        layer,
                        decision,
                        applied=False,
                        error=str(exc),
                    )
                )
                continue

            applied_count += 1
            items.append(_execution_item(item_id, layer, decision, applied=True))

        status = "dry_run" if dry_run else "completed"
        if failed_count:
            status = "degraded"

        return LifecycleExecutionReport(
            status=status,
            dry_run=dry_run,
            evaluated_count=evaluated_count,
            retained_count=retained_count,
            planned_count=planned_count,
            applied_count=applied_count,
            failed_count=failed_count,
            items=tuple(items),
        )

    def compute_metrics(
        self,
        *,
        layers: list[str] | None = None,
    ) -> OperationalMetrics:
        """Compute operational Memory OS health scores from stored memory."""
        items = list(self._iter_items(layers))
        total = len(items)
        layer_counts = _counts(str(item.get("layer") or "unknown") for item in items)
        stage_counts = _counts(str(item.get("stage") or "unknown") for item in items)
        recovery = self.recover_store(dry_run=True, layers=layers, reindex=False)

        if total == 0:
            return OperationalMetrics(
                context_continuity_score=0.0,
                retrieval_relevance_score=0.0,
                historical_awareness_accuracy=0.0,
                compression_preservation_quality=0.0,
                lifecycle_consistency_rate=1.0 if recovery.scanned_count == 0 else 0.0,
                persistent_memory_stability=_ratio(
                    recovery.readable_count,
                    recovery.scanned_count,
                    empty=1.0,
                ),
                item_count=0,
                layer_counts=layer_counts,
                stage_counts=stage_counts,
                issue_count=len(recovery.issues),
            )

        context_count = sum(1 for item in items if _has_continuity_metadata(item))
        retrieval_scores = [_retrieval_relevance_score(item) for item in items]
        historical_count = sum(1 for item in items if _has_historical_context(item))
        compression_score = _compression_preservation_score(items)
        lifecycle_score = 1.0 - _ratio(len(recovery.issues), max(1, recovery.readable_count))
        stability_score = _ratio(recovery.readable_count, recovery.scanned_count, empty=1.0)

        return OperationalMetrics(
            context_continuity_score=_round_score(_ratio(context_count, total)),
            retrieval_relevance_score=_round_score(sum(retrieval_scores) / total),
            historical_awareness_accuracy=_round_score(_ratio(historical_count, total)),
            compression_preservation_quality=_round_score(compression_score),
            lifecycle_consistency_rate=_round_score(lifecycle_score),
            persistent_memory_stability=_round_score(stability_score),
            item_count=total,
            layer_counts=layer_counts,
            stage_counts=stage_counts,
            issue_count=len(recovery.issues),
        )

    def record_lifecycle_report(
        self,
        report: LifecycleExecutionReport,
        *,
        label: str | None = None,
    ) -> Path:
        """Persist a lifecycle report as durable operational evidence."""
        payload = {
            "kind": "lifecycle_report",
            "recorded_at": _now_iso(),
            "report": report.to_dict(),
        }
        path = self._evidence_path("lifecycle", label or report.status)
        _write_json(path, payload)
        _write_json(self._evidence_dir / "latest-lifecycle.json", payload)
        return path

    def record_metrics_snapshot(
        self,
        metrics: OperationalMetrics | None = None,
        *,
        layers: list[str] | None = None,
        label: str | None = None,
    ) -> Path:
        """Persist metrics as a snapshot and append trendable history."""
        metrics = metrics or self.compute_metrics(layers=layers)
        payload = {
            "kind": "metrics_snapshot",
            "recorded_at": _now_iso(),
            "metrics": metrics.to_dict(),
        }
        path = self._evidence_path("metrics", label or "snapshot")
        _write_json(path, payload)
        _write_json(self._evidence_dir / "latest-metrics.json", payload)
        self._append_jsonl(self._evidence_dir / "metrics-history.jsonl", payload)
        return path

    def metric_history(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Read persisted Memory OS metric snapshots."""
        path = self._evidence_dir / "metrics-history.jsonl"
        if not path.exists():
            return []

        records: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []

        if limit is not None:
            return records[-limit:]
        return records

    def validate_health(
        self,
        *,
        layers: list[str] | None = None,
        metrics: OperationalMetrics | None = None,
        thresholds: OperationalHealthThresholds | None = None,
        min_score: float | None = None,
    ) -> OperationalValidationReport:
        """Validate operational health against calibrated score gates."""
        metrics = metrics or self.compute_metrics(layers=layers)
        thresholds = thresholds or (
            OperationalHealthThresholds.uniform(min_score)
            if min_score is not None
            else OperationalHealthThresholds()
        )
        score_payload = metrics.to_dict()["scores"]
        threshold_payload = thresholds.to_dict()
        gates = tuple(
            HealthGateResult(
                name=name,
                actual=_round_score(float(score_payload[name])),
                threshold=_round_score(float(threshold_payload[name])),
                passed=float(score_payload[name]) >= float(threshold_payload[name]),
            )
            for name in SCORE_NAMES
        )
        passed = all(gate.passed for gate in gates)
        return OperationalValidationReport(
            status="passed" if passed else "failed",
            passed=passed,
            gates=gates,
            metrics=metrics,
            trend=self._metric_trend(metrics),
        )

    def record_validation_report(
        self,
        report: OperationalValidationReport,
        *,
        label: str | None = None,
    ) -> Path:
        """Persist a health validation report as operational evidence."""
        payload = {
            "kind": "health_validation",
            "recorded_at": _now_iso(),
            "report": report.to_dict(),
        }
        path = self._evidence_path("health", label or report.status)
        _write_json(path, payload)
        _write_json(self._evidence_dir / "latest-health.json", payload)
        return path

    def record_recovery_report(
        self,
        report: MemoryRecoveryReport,
        *,
        label: str | None = None,
    ) -> Path:
        """Persist a recovery report as operational evidence."""
        payload = {
            "kind": "recovery_report",
            "recorded_at": _now_iso(),
            "report": report.to_dict(),
        }
        path = self._evidence_path("recovery", label or report.status)
        _write_json(path, payload)
        _write_json(self._evidence_dir / "latest-recovery.json", payload)
        return path

    def recover_store(
        self,
        *,
        dry_run: bool = True,
        layers: list[str] | None = None,
        reindex: bool = True,
    ) -> MemoryRecoveryReport:
        """Detect and optionally repair recoverable memory store issues."""
        issues: list[MemoryRecoveryIssue] = []
        scanned_count = 0
        readable_count = 0
        corrupt_count = 0
        repaired_count = 0
        reindexed_count = 0

        for layer, path in self._iter_memory_paths(layers):
            scanned_count += 1
            try:
                item = self._parse_path(path)
            except Exception as exc:
                corrupt_count += 1
                issues.append(
                    MemoryRecoveryIssue(
                        path=str(path),
                        code="parse_error",
                        message=str(exc),
                        layer=layer,
                        repaired=False,
                    )
                )
                continue

            readable_count += 1
            item["_path"] = str(path)
            corrections, item_issues = self._repair_metadata_plan(item, layer, path)

            if corrections and not dry_run:
                self._store.update(str(path), metadata_updates=corrections)
                repaired_count += len(item_issues)

            elif corrections:
                repaired_count += 0

            repaired = bool(corrections and not dry_run)
            for code, message in item_issues:
                issues.append(
                    MemoryRecoveryIssue(
                        path=str(path),
                        code=code,
                        message=message,
                        item_id=str(corrections.get("id") or item.get("id") or path.stem),
                        layer=str(corrections.get("layer") or item.get("layer") or layer),
                        repaired=repaired,
                    )
                )

            if reindex and not dry_run:
                merged = dict(item)
                merged.update(corrections)
                item_id = _item_id(merged)
                metadata = {
                    key: value
                    for key, value in merged.items()
                    if key not in {"content", "_path"}
                }
                self._fts.index_item(item_id, str(merged.get("content") or ""), metadata)
                reindexed_count += 1

        status = "dry_run" if dry_run else "completed"
        if corrupt_count:
            status = "degraded" if not dry_run else "dry_run"

        return MemoryRecoveryReport(
            status=status,
            dry_run=dry_run,
            scanned_count=scanned_count,
            readable_count=readable_count,
            corrupt_count=corrupt_count,
            repaired_count=repaired_count,
            reindexed_count=reindexed_count,
            issues=tuple(issues),
        )

    def _metric_trend(self, metrics: OperationalMetrics) -> dict[str, Any]:
        history = self.metric_history(limit=1)
        current = metrics.to_dict()["scores"]
        if not history:
            return {
                "status": "no_history",
                "previous_recorded_at": None,
                "deltas": {},
            }

        previous_record = history[-1]
        previous_scores = (
            previous_record.get("metrics", {}).get("scores", {})
            if isinstance(previous_record, dict)
            else {}
        )
        deltas = {
            name: _round_delta(
                float(current.get(name, 0.0)) - float(previous_scores.get(name, 0.0))
            )
            for name in SCORE_NAMES
            if name in previous_scores
        }
        improving = sum(1 for value in deltas.values() if value > 0)
        declining = sum(1 for value in deltas.values() if value < 0)
        if declining:
            status = "declining"
        elif improving:
            status = "improving"
        else:
            status = "stable"

        return {
            "status": status,
            "previous_recorded_at": previous_record.get("recorded_at"),
            "deltas": deltas,
        }

    def _evidence_path(self, category: str, label: str) -> Path:
        stamp = _now_iso().replace(":", "").replace("-", "")
        safe_label = _safe_label(label)
        return self._evidence_dir / category / f"{stamp}-{safe_label}.json"

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def _apply_lifecycle_decision(
        self,
        item: dict[str, Any],
        decision: LifecycleDecision,
    ) -> None:
        item_id = _item_id(item)

        if decision.action is LifecycleAction.PROMOTE:
            self._gateway.promote(
                item_id=item_id,
                target_layer=decision.target_layer,
                run_id=item.get("run_id"),
                session_id=item.get("session_id"),
                force=True,
            )
            promoted = self._store.read(item_id)
            self._store.update(str(promoted["_path"]), metadata_updates=decision.metadata_updates)
            updated = dict(promoted)
            updated.update(decision.metadata_updates)
            metadata = {
                key: value
                for key, value in updated.items()
                if key not in {"content", "_path"}
            }
            self._fts.index_item(item_id, str(promoted.get("content") or ""), metadata)
            return

        metadata_updates = dict(decision.metadata_updates)
        if decision.action in {LifecycleAction.SUMMARIZE, LifecycleAction.COMPRESS}:
            summary = self._lifecycle.summarize_item(item)
            metadata_updates["summary"] = summary
            metadata_updates["summary_strategy"] = "deterministic-operational-v1"
        if decision.action is LifecycleAction.COMPRESS:
            metadata_updates["compression_strategy"] = "continuity-metadata-v1"
            metadata_updates["compression_preserves_content"] = True

        self._store.update(str(item["_path"]), metadata_updates=metadata_updates)
        updated = dict(item)
        updated.update(metadata_updates)
        metadata = {
            key: value
            for key, value in updated.items()
            if key not in {"content", "_path"}
        }
        self._fts.index_item(item_id, str(item.get("content") or ""), metadata)

        if hasattr(self._gateway, "log"):
            self._gateway.log(
                f"lifecycle_{decision.action.value}",
                item_id,
                str(item.get("layer") or "unknown"),
                {
                    "reason": decision.reason,
                    "target_layer": decision.target_layer,
                    "target_stage": decision.target_stage,
                },
            )

    def _iter_items(self, layers: list[str] | None = None) -> Iterable[dict[str, Any]]:
        for layer in _normalize_layers(layers):
            for item in self._store.iter_layer_items(layer):
                yield item

    def _iter_memory_paths(
        self,
        layers: list[str] | None = None,
    ) -> Iterable[tuple[str, Path]]:
        selected = set(_normalize_layers(layers))

        if "transient" in selected:
            yield from _glob_layer(self._root / TRANSIENT_PATH, "transient")

        for layer, rel_path in LAYER_STATIC_PATHS.items():
            if layer in selected:
                yield from _glob_layer(self._root / rel_path, layer)

        runs_root = self._root / ".agent" / "runs"
        if runs_root.exists():
            for run_dir in runs_root.iterdir():
                if not run_dir.is_dir():
                    continue
                if "ephemeral" in selected:
                    yield from _glob_layer(run_dir / "scratch", "ephemeral")
                if "working" in selected:
                    yield from _glob_layer(run_dir / "working", "working")

        sessions_root = self._root / ".agent" / "sessions"
        if "session" in selected and sessions_root.exists():
            yield from _glob_layer(sessions_root, "session", recursive=True)

    def _parse_path(self, path: Path) -> dict[str, Any]:
        if hasattr(self._store, "parse_file"):
            return self._store.parse_file(path)

        post = frontmatter.load(str(path))
        item = dict(post.metadata)
        item["content"] = post.content
        item["_path"] = str(path)
        return item

    def _repair_metadata_plan(
        self,
        item: dict[str, Any],
        path_layer: str,
        path: Path,
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        corrections: dict[str, Any] = {}
        issues: list[tuple[str, str]] = []
        item_id = item.get("id")
        layer = item.get("layer")
        stage = item.get("stage")
        trust = item.get("trust_level")
        quality = item.get("quality_score")
        access_count = item.get("access_count")
        tags = item.get("tags")
        content = str(item.get("content") or "")
        content_hash = item.get("content_hash")

        if not item_id:
            corrections["id"] = path.stem
            issues.append(("missing_id", "metadata id was missing"))

        if layer not in OPERATIONAL_LAYERS:
            corrections["layer"] = path_layer
            code = "missing_layer" if not layer else "invalid_layer"
            issues.append((code, "metadata layer did not match a supported layer"))

        if stage not in VALID_STAGES:
            corrections["stage"] = "stored"
            code = "missing_stage" if not stage else "invalid_stage"
            issues.append((code, "metadata stage did not match lifecycle stages"))

        if trust not in {trust_level.value for trust_level in TrustLevel}:
            corrections["trust_level"] = normalize_trust_level(trust).value
            code = "missing_trust_level" if not trust else "invalid_trust_level"
            issues.append((code, "metadata trust_level was missing or invalid"))

        quality_value = _safe_float(quality)
        if quality_value is None or not 0.0 <= quality_value <= 1.0:
            corrections["quality_score"] = 0.8 if quality_value is None else _clamp(quality_value)
            code = "missing_quality_score" if quality is None else "invalid_quality_score"
            issues.append((code, "metadata quality_score was missing or outside 0.0-1.0"))

        access_value = _safe_int(access_count)
        if access_value is None or access_value < 0:
            corrections["access_count"] = 0
            code = "missing_access_count" if access_count is None else "invalid_access_count"
            issues.append((code, "metadata access_count was missing or invalid"))

        if not isinstance(tags, list):
            corrections["tags"] = [] if tags is None else [str(tags)]
            code = "missing_tags" if tags is None else "invalid_tags"
            issues.append((code, "metadata tags was missing or not a list"))

        if not isinstance(content_hash, str) or len(content_hash) != 64:
            corrections["content_hash"] = _content_hash(content)
            code = "missing_content_hash" if not content_hash else "invalid_content_hash"
            issues.append((code, "metadata content_hash was missing or invalid"))

        return corrections, issues


def _execution_item(
    item_id: str,
    layer: str,
    decision: LifecycleDecision,
    *,
    applied: bool,
    error: str | None = None,
) -> LifecycleExecutionItem:
    return LifecycleExecutionItem(
        item_id=item_id,
        layer=layer,
        action=decision.action.value,
        reason=decision.reason,
        target_layer=decision.target_layer,
        target_stage=decision.target_stage,
        applied=applied,
        error=error,
    )


def _normalize_layers(layers: list[str] | None) -> tuple[str, ...]:
    if not layers:
        return OPERATIONAL_LAYERS
    selected = []
    for layer in layers:
        layer = layer.strip()
        if layer and layer in OPERATIONAL_LAYERS and layer not in selected:
            selected.append(layer)
    return tuple(selected)


def _glob_layer(
    directory: Path,
    layer: str,
    *,
    recursive: bool = False,
) -> Iterable[tuple[str, Path]]:
    if not directory.exists():
        return
    pattern = "**/*.md" if recursive else "*.md"
    for path in sorted(directory.glob(pattern)):
        if path.is_file():
            yield layer, path


def _item_id(item: dict[str, Any]) -> str:
    raw_path = item.get("_path")
    return str(item.get("id") or item.get("item_id") or (Path(raw_path).stem if raw_path else "unknown"))


def _path_layer(path_value: Any) -> str | None:
    if not path_value:
        return None
    path = Path(str(path_value))
    parts = set(path.parts)
    if "scratch" in parts:
        return "ephemeral"
    if "working" in parts:
        return "working"
    if "sessions" in parts:
        return "session"
    for layer, rel_path in LAYER_STATIC_PATHS.items():
        if set(Path(rel_path).parts).issubset(parts):
            return layer
    if "transient" in parts:
        return "transient"
    return None


def _has_continuity_metadata(item: dict[str, Any]) -> bool:
    if not str(item.get("content") or "").strip():
        return False
    if item.get("workflow_id") or item.get("session_id") or item.get("run_id") or item.get("related_to"):
        return True
    tags = item.get("tags") or []
    return bool(tags)


def _has_historical_context(item: dict[str, Any]) -> bool:
    layer = str(item.get("layer") or "")
    stage = str(item.get("stage") or "")
    if layer in {"project", "global"} and item.get("created_at"):
        return True
    if stage in {"summarized", "compressed", "archived", "promoted"}:
        return True
    return _safe_int(item.get("access_count")) is not None and int(item.get("access_count") or 0) > 0


def _retrieval_relevance_score(item: dict[str, Any]) -> float:
    quality = _clamp(_safe_float(item.get("quality_score")) if item.get("quality_score") is not None else 0.8)
    trust = TRUST_RANK[normalize_trust_level(item.get("trust_level"))] / max(TRUST_RANK.values())
    access_count = max(0, _safe_int(item.get("access_count")) or 0)
    history = min(1.0, access_count / 5)
    return quality * 0.6 + trust * 0.25 + history * 0.15


def _compression_preservation_score(items: list[dict[str, Any]]) -> float:
    managed = [
        item for item in items
        if str(item.get("stage") or "") in {"summarized", "compressed"}
    ]
    if managed:
        preserved = sum(
            1
            for item in managed
            if str(item.get("content") or "").strip() and item.get("summary")
        )
        return _ratio(preserved, len(managed))

    long_items = [item for item in items if len(str(item.get("content") or "")) >= 900]
    if not long_items:
        return 1.0
    return 1.0 - _ratio(len(long_items), len(items))


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _ratio(numerator: int | float, denominator: int | float, *, empty: float = 0.0) -> float:
    if denominator == 0:
        return empty
    return _clamp(float(numerator) / float(denominator))


def _round_score(value: float) -> float:
    return round(_clamp(value), 6)


def _round_delta(value: float) -> float:
    return round(float(value), 6)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _content_hash(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    normalized = re.sub(r"\s+", " ", normalized.strip()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "snapshot").strip().lower())
    label = label.strip("-")
    return label or "snapshot"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
