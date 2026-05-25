"""Tests for Memory OS operational execution, metrics, and recovery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli
from core.gateway import MemoryGateway
from core.operations import MemoryOperationsEngine


@pytest.fixture
def operations_repo(tmp_path: Path) -> Path:
    """Create a minimal repository for operational memory tests."""
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for dirname in ["runs", "sessions", "state", "reports", "tools", "transient"]:
        (agent / dirname).mkdir(parents=True)

    policy = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 1.0},
            },
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 0.8},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 1.0, "access_count": 2, "quality_score": 0.8},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 1.0, "access_count": 3, "quality_score": 0.85},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 1.0, "access_count": 5, "quality_score": 0.9},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 1.0, "access_count": 5, "quality_score": 0.9},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {
            "allowed_stages": [
                "stored",
                "retrieved",
                "used",
                "validated",
                "summarized",
                "compressed",
            ]
        },
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")

    return tmp_path


def test_lifecycle_dry_run_reports_without_mutating(operations_repo: Path) -> None:
    """Lifecycle dry-run reports the next action while preserving current state."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="summary-candidate",
        content="summary candidate " * 80,
        tags=["workflow"],
        quality_score=0.9,
        extra_metadata={"trust_level": "verified"},
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).run_lifecycle(
        dry_run=True,
        layers=["project"],
    )

    assert report.status == "dry_run"
    assert report.planned_count == 1
    assert report.applied_count == 0
    assert report.items[0].action == "summarize"
    assert gateway._store.read("summary-candidate")["stage"] == "stored"
    assert "summary" not in gateway._store.read("summary-candidate")


def test_lifecycle_apply_compresses_metadata_without_destroying_content(
    operations_repo: Path,
) -> None:
    """Compression lifecycle keeps source content and adds continuity metadata."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    original_content = ("compression candidate operational continuity " * 80).strip()
    gateway.capture(
        layer="project",
        item_id="compression-candidate",
        content=original_content,
        tags=["workflow", "continuity"],
        quality_score=0.9,
        extra_metadata={"trust_level": "verified"},
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).run_lifecycle(
        dry_run=False,
        layers=["project"],
    )

    item = gateway._store.read("compression-candidate")
    assert report.applied_count == 1
    assert report.items[0].action == "compress"
    assert item["stage"] == "compressed"
    assert item["content"] == original_content
    assert item["summary"].startswith("compression-candidate")
    assert item["compression_preserves_content"] is True


def test_lifecycle_apply_promotes_operational_memory(operations_repo: Path) -> None:
    """Promotion lifecycle moves trusted, frequently used memory to the next tier."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="session",
        item_id="promotion-candidate",
        content="Short trusted operational memory for promotion.",
        tags=["workflow"],
        quality_score=0.95,
        session_id="ops-session",
        extra_metadata={"trust_level": "verified", "access_count": 6},
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).run_lifecycle(
        dry_run=False,
        layers=["session"],
    )

    item = gateway._store.read("promotion-candidate")
    assert report.applied_count == 1
    assert report.items[0].action == "promote"
    assert item["layer"] == "project"
    assert item["stage"] == "promoted"
    assert item["lifecycle_action"] == "promote"


def test_operational_metrics_expose_memory_os_scores(operations_repo: Path) -> None:
    """Metrics expose continuity, relevance, history, lifecycle, and stability."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="metric-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "metrics-flow",
            "access_count": 4,
        },
        no_classify=True,
    )

    metrics = MemoryOperationsEngine(gateway).compute_metrics(layers=["project"])
    payload = metrics.to_dict()

    assert metrics.item_count == 1
    assert set(payload["scores"]) == {
        "context_continuity_score",
        "retrieval_relevance_score",
        "historical_awareness_accuracy",
        "compression_preservation_quality",
        "lifecycle_consistency_rate",
        "persistent_memory_stability",
    }
    assert all(0.0 <= score <= 1.0 for score in payload["scores"].values())
    assert payload["scores"]["context_continuity_score"] == 1.0
    assert payload["scores"]["persistent_memory_stability"] == 1.0


def test_operational_evidence_records_reports_and_metric_history(
    operations_repo: Path,
) -> None:
    """Lifecycle and metrics evidence is persisted for audit and trend analysis."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="evidence-memory",
        content="evidence candidate " * 80,
        tags=["workflow"],
        quality_score=0.91,
        extra_metadata={"trust_level": "verified", "workflow_id": "evidence-flow"},
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)

    lifecycle_report = engine.run_lifecycle(dry_run=True, layers=["project"])
    lifecycle_path = engine.record_lifecycle_report(lifecycle_report)
    metrics = engine.compute_metrics(layers=["project"])
    metrics_path = engine.record_metrics_snapshot(metrics)
    history = engine.metric_history()

    assert lifecycle_path.exists()
    assert metrics_path.exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-lifecycle.json").exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-metrics.json").exists()
    assert len(history) == 1
    assert history[0]["kind"] == "metrics_snapshot"
    assert history[0]["metrics"]["scores"]["context_continuity_score"] == 1.0


def test_managed_compression_job_dry_run_preserves_source_memories(
    operations_repo: Path,
) -> None:
    """Compression jobs can plan continuity pages without mutating source memory."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="compress-source-a",
        content="Architecture decision: persistent memory contracts preserve continuity.",
        tags=["architecture", "continuity"],
        quality_score=0.96,
        extra_metadata={"trust_level": "verified", "workflow_id": "compress-flow"},
        no_classify=True,
    )
    gateway.capture(
        layer="project",
        item_id="compress-source-b",
        content="Workflow result: health validation keeps operational memory measurable.",
        tags=["workflow", "health"],
        quality_score=0.93,
        extra_metadata={"trust_level": "verified", "workflow_id": "compress-flow"},
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).run_compression_job(
        dry_run=True,
        layers=["project"],
        query="persistent memory continuity",
        token_budget=256,
        label="dry-run",
    )

    assert report.status == "dry_run"
    assert report.input_count == 2
    assert report.page_count == 1
    assert report.applied_count == 0
    assert set(report.pages[0].source_item_ids) == {"compress-source-a", "compress-source-b"}
    assert gateway._store.read("compress-source-a")["stage"] == "stored"
    artifacts = [
        item
        for item in gateway._store.iter_layer_items("project")
        if item.get("memory_os_artifact") == "continuity_page"
    ]
    assert artifacts == []


def test_managed_compression_job_writes_searchable_continuity_artifacts(
    operations_repo: Path,
) -> None:
    """Compression jobs write durable continuity pages without destroying sources."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="artifact-source-a",
        content="Historical decision: compression pages preserve source memory identity.",
        tags=["history", "compression"],
        quality_score=0.97,
        extra_metadata={"trust_level": "verified", "workflow_id": "artifact-flow"},
        no_classify=True,
    )
    gateway.capture(
        layer="project",
        item_id="artifact-source-b",
        content="Operational note: continuity pages remain searchable after compression.",
        tags=["workflow", "search"],
        quality_score=0.94,
        extra_metadata={"trust_level": "verified", "workflow_id": "artifact-flow"},
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)

    report = engine.run_compression_job(
        dry_run=False,
        layers=["project"],
        target_layer="project",
        query="compression continuity searchable",
        token_budget=256,
        label="artifact",
    )
    evidence_path = engine.record_compression_report(report)

    assert report.status == "completed"
    assert report.applied_count == 1
    artifact = gateway._store.read(report.pages[0].artifact_id)
    assert artifact["stage"] == "compressed"
    assert artifact["memory_os_artifact"] == "continuity_page"
    assert set(artifact["source_item_ids"]) == {"artifact-source-a", "artifact-source-b"}
    assert "artifact-source-a" in artifact["content"]
    assert gateway._store.read("artifact-source-a")["content"].startswith("Historical decision")
    assert evidence_path.exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-compression.json").exists()
    results = gateway.search("continuity pages searchable", layers=["project"], limit=5)
    assert report.pages[0].artifact_id in {result["item_id"] for result in results}


def test_health_validation_tracks_trend_from_metric_history(
    operations_repo: Path,
) -> None:
    """Validation gates compare current health against prior metric snapshots."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="trend-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "trend-flow",
            "access_count": 0,
        },
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)
    engine.record_metrics_snapshot(engine.compute_metrics(layers=["project"]))

    item = gateway._store.read("trend-memory")
    gateway._store.update(item["_path"], metadata_updates={"access_count": 5})
    report = engine.validate_health(layers=["project"], min_score=0.7)
    validation_path = engine.record_validation_report(report)

    assert report.passed is True
    assert report.status == "passed"
    assert report.backend_health is not None
    assert report.backend_health.status == "ok"
    assert {gate.name for gate in report.gates} >= {"retrieval_backend_health"}
    assert report.trend["status"] == "improving"
    assert report.trend["deltas"]["retrieval_relevance_score"] > 0
    assert validation_path.exists()


def test_metric_calibration_persists_empirical_health_baseline(
    operations_repo: Path,
) -> None:
    """Calibration derives thresholds from observed metric history."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="calibration-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "calibration-flow",
            "access_count": 5,
        },
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)
    engine.record_metrics_snapshot(engine.compute_metrics(layers=["project"]))

    calibration = engine.calibrate_health(
        layers=["project"],
        floor=0.7,
        tolerance=0.02,
    )
    calibration_path = engine.record_calibration_report(calibration)
    validation = engine.validate_health(layers=["project"], calibrated=True)

    assert calibration.status == "calibrated"
    assert calibration.sample_count == 2
    assert calibration.thresholds.retrieval_relevance_score >= 0.7
    assert calibration_path.exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-calibration.json").exists()
    assert validation.passed is True


def test_calibrated_health_validation_detects_metric_regression(
    operations_repo: Path,
) -> None:
    """Calibrated validation fails when current metrics regress below the baseline."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="regression-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.98,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "regression-flow",
            "access_count": 5,
        },
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)
    engine.record_metrics_snapshot(engine.compute_metrics(layers=["project"]))
    engine.record_calibration_report(
        engine.calibrate_health(
            layers=["project"],
            floor=0.7,
            tolerance=0.02,
        )
    )

    item = gateway._store.read("regression-memory")
    gateway._store.update(
        item["_path"],
        metadata_updates={
            "quality_score": 0.1,
            "trust_level": "unverified",
            "access_count": 0,
        },
    )
    validation = engine.validate_health(layers=["project"], calibrated=True)
    gates = {gate.name: gate for gate in validation.gates}

    assert validation.passed is False
    assert validation.status == "failed"
    assert gates["retrieval_relevance_score"].passed is False
    assert gates["retrieval_relevance_score"].threshold > gates["retrieval_relevance_score"].actual


def test_health_validation_fails_when_operational_scores_are_below_threshold(
    operations_repo: Path,
) -> None:
    """Health validation fails closed when continuity evidence is insufficient."""
    gateway = MemoryGateway(repo_root=str(operations_repo))
    report = MemoryOperationsEngine(gateway).validate_health(
        layers=["project"],
        min_score=0.85,
    )
    gates = {gate.name: gate for gate in report.gates}

    assert report.passed is False
    assert report.status == "failed"
    assert gates["context_continuity_score"].passed is False
    assert gates["retrieval_relevance_score"].passed is False


def test_health_validation_fails_on_configured_backend_degradation(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured retrieval backend degradation is a Memory OS health failure."""
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "invalid_backend")
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="backend-health-gate",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "backend-health-gate",
            "access_count": 5,
        },
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).validate_health(
        layers=["project"],
        min_score=0.0,
    )
    gates = {gate.name: gate for gate in report.gates}

    assert report.passed is False
    assert report.status == "failed"
    assert report.backend_health is not None
    assert report.backend_health.status == "degraded"
    assert gates["retrieval_backend_health"].passed is False
    assert report.to_dict()["backend_health"]["backends"][1]["status"] == "unsupported"


def test_readiness_audit_reports_missing_evidence_as_actionable_gap(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness audit aggregates health and reports durable evidence gaps."""
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "none")
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="readiness-gap-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "readiness-gap",
            "access_count": 5,
        },
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).audit_readiness(
        layers=["project"],
        min_score=0.7,
    )
    gap_codes = {gap.code for gap in report.gaps}

    assert report.ready is True
    assert report.status == "needs_attention"
    assert report.validation is not None
    assert report.validation.passed is True
    assert "evidence_missing:metrics" in gap_codes
    assert "evidence_missing:health" in gap_codes
    assert "evidence_missing:backends" in gap_codes


def test_readiness_audit_records_consolidated_report_when_evidence_is_fresh(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh evidence plus passing health produces a persisted readiness report."""
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "none")
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="readiness-ready-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "readiness-ready",
            "access_count": 5,
        },
        no_classify=True,
    )
    engine = MemoryOperationsEngine(gateway)
    metrics = engine.compute_metrics(layers=["project"])
    engine.record_metrics_snapshot(metrics)
    engine.record_backend_health_report(engine.retrieval_backend_health())
    engine.record_validation_report(
        engine.validate_health(layers=["project"], metrics=metrics, min_score=0.7)
    )

    report = engine.audit_readiness(layers=["project"], min_score=0.7)
    readiness_path = engine.record_readiness_report(report)
    second_report = engine.audit_readiness(layers=["project"], min_score=0.7)
    engine.record_readiness_report(second_report)

    assert report.ready is True
    assert report.status == "ready"
    assert report.thresholds["max_evidence_age_hours"] == 24.0
    assert report.thresholds["validation_gates"]["retrieval_backend_health"] == 1.0
    assert report.trend["status"] == "no_history"
    assert second_report.trend["status"] == "stable"
    assert second_report.trend["gap_count_delta"] == 0
    assert second_report.trend["score_deltas"]["retrieval_relevance_score"] == 0.0
    assert {item.kind: item.status for item in report.evidence}["metrics"] == "fresh"
    assert readiness_path.exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-readiness.json").exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "readiness-history.jsonl").exists()
    assert len(engine.readiness_history()) == 2


def test_readiness_audit_fails_on_backend_degradation(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness audit is not ready when configured retrieval backends are unhealthy."""
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "invalid_backend")
    gateway = MemoryGateway(repo_root=str(operations_repo))
    gateway.capture(
        layer="project",
        item_id="readiness-backend-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "readiness-backend",
            "access_count": 5,
        },
        no_classify=True,
    )

    report = MemoryOperationsEngine(gateway).audit_readiness(
        layers=["project"],
        min_score=0.0,
    )

    assert report.ready is False
    assert report.status == "not_ready"
    assert any(gap.code == "gate_failed:retrieval_backend_health" for gap in report.gaps)


def test_recovery_dry_run_detects_metadata_and_parse_issues(
    operations_repo: Path,
) -> None:
    """Recovery dry-run detects repairable metadata gaps and corrupt files."""
    recoverable = operations_repo / "wiki" / "projects" / "recoverable-memory.md"
    recoverable.write_text(
        "---\nlayer: project\n---\nRecoverable memory content.\n",
        encoding="utf-8",
    )
    corrupt = operations_repo / "wiki" / "projects" / "corrupt-memory.md"
    corrupt.write_text(
        "---\nid: corrupt-memory\nlayer: project\ntags: [broken\n---\nBroken.\n",
        encoding="utf-8",
    )

    gateway = MemoryGateway(repo_root=str(operations_repo))
    report = MemoryOperationsEngine(gateway).recover_store(
        dry_run=True,
        layers=["project"],
    )
    codes = {issue.code for issue in report.issues}

    assert report.status == "dry_run"
    assert report.scanned_count == 2
    assert report.readable_count == 1
    assert report.corrupt_count == 1
    assert "parse_error" in codes
    assert "missing_id" in codes
    assert "missing_content_hash" in codes
    assert "id:" not in recoverable.read_text(encoding="utf-8")


def test_recovery_apply_repairs_metadata_and_reindexes(
    operations_repo: Path,
) -> None:
    """Recovery apply repairs metadata and restores searchability."""
    recoverable = operations_repo / "wiki" / "projects" / "recoverable-memory.md"
    recoverable.write_text(
        "---\nlayer: project\n---\nRecoverable operational memory content.\n",
        encoding="utf-8",
    )

    gateway = MemoryGateway(repo_root=str(operations_repo))
    engine = MemoryOperationsEngine(gateway)
    report = engine.recover_store(
        dry_run=False,
        layers=["project"],
    )
    recovery_path = engine.record_recovery_report(report)
    item = gateway._store.read(str(recoverable))
    results = gateway.search("recoverable operational memory", layers=["project"])

    assert report.status == "completed"
    assert report.repaired_count > 0
    assert report.reindexed_count == 1
    assert item["id"] == "recoverable-memory"
    assert item["stage"] == "stored"
    assert item["trust_level"] == "observed"
    assert len(item["content_hash"]) == 64
    assert results[0]["item_id"] == "recoverable-memory"
    assert recovery_path.exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-recovery.json").exists()


def test_memory_operations_cli_and_capabilities(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI and provider metadata expose operational Memory OS surfaces."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(operations_repo))
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "none")
    runner = CliRunner()
    MemoryGateway(repo_root=str(operations_repo)).capture(
        layer="project",
        item_id="cli-health-memory",
        content="Workflow-aware operational memory preserves historical continuity.",
        tags=["workflow", "continuity"],
        quality_score=0.96,
        extra_metadata={
            "trust_level": "verified",
            "workflow_id": "cli-health-flow",
            "access_count": 5,
        },
        no_classify=True,
    )

    metrics_result = runner.invoke(cli, ["memory-metrics", "--record", "--json"])
    backends_result = runner.invoke(cli, ["memory-backends", "--record", "--json"])
    readiness_result = runner.invoke(cli, ["memory-readiness", "--min-score", "0.7", "--record", "--json"])
    compress_result = runner.invoke(
        cli,
        [
            "memory-compress",
            "--apply",
            "--layer",
            "project",
            "--query",
            "workflow continuity",
            "--token-budget",
            "256",
            "--record",
            "--json",
        ],
    )
    validate_result = runner.invoke(cli, ["memory-validate", "--min-score", "0.7", "--record", "--json"])
    calibrate_result = runner.invoke(cli, ["memory-calibrate", "--floor", "0.7", "--record", "--json"])
    calibrated_validate_result = runner.invoke(cli, ["memory-validate", "--calibrated", "--record", "--json"])
    capabilities_result = runner.invoke(cli, ["capabilities", "--json"])

    assert metrics_result.exit_code == 0, metrics_result.output
    metrics_payload = json.loads(metrics_result.output)
    assert metrics_payload["status"] == "ok"
    assert Path(metrics_payload["evidence_path"]).exists()
    assert backends_result.exit_code == 0, backends_result.output
    backends_payload = json.loads(backends_result.output)
    assert backends_payload["status"] == "ok"
    assert backends_payload["retrieval_contract"] == "fts-primary-vector-optional-grep-fallback"
    assert Path(backends_payload["evidence_path"]).exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "latest-backends.json").exists()
    assert readiness_result.exit_code == 0, readiness_result.output
    readiness_payload = json.loads(readiness_result.output)
    assert readiness_payload["ready"] is True
    assert readiness_payload["status"] in {"ready", "needs_attention"}
    assert readiness_payload["thresholds"]["required_evidence"] == ["metrics", "health", "backends"]
    assert readiness_payload["trend"]["status"] == "no_history"
    assert readiness_payload["validation"]["status"] == "passed"
    assert Path(readiness_payload["evidence_path"]).exists()
    assert (operations_repo / ".agent" / "reports" / "memory-os" / "readiness-history.jsonl").exists()
    assert compress_result.exit_code == 0, compress_result.output
    compress_payload = json.loads(compress_result.output)
    assert compress_payload["status"] == "completed"
    assert compress_payload["applied_count"] >= 1
    assert Path(compress_payload["evidence_path"]).exists()
    assert validate_result.exit_code == 0, validate_result.output
    validate_payload = json.loads(validate_result.output)
    assert validate_payload["status"] == "passed"
    assert validate_payload["backend_health"]["status"] == "ok"
    assert Path(validate_payload["evidence_path"]).exists()
    assert calibrate_result.exit_code == 0, calibrate_result.output
    calibrate_payload = json.loads(calibrate_result.output)
    assert calibrate_payload["status"] in {"calibrated", "bootstrapped"}
    assert Path(calibrate_payload["evidence_path"]).exists()
    assert calibrated_validate_result.exit_code == 0, calibrated_validate_result.output
    calibrated_payload = json.loads(calibrated_validate_result.output)
    assert calibrated_payload["status"] == "passed"
    assert Path(calibrated_payload["evidence_path"]).exists()
    assert capabilities_result.exit_code == 0, capabilities_result.output
    capabilities = json.loads(capabilities_result.output)
    assert capabilities["capability_status"]["lifecycle_execution"] == "supported"
    assert capabilities["capability_status"]["operational_metrics"] == "supported"
    assert capabilities["capability_status"]["memory_recovery"] == "supported"
    assert capabilities["capability_status"]["operational_evidence"] == "supported"
    assert capabilities["capability_status"]["health_validation"] == "supported"
    assert capabilities["capability_status"]["autonomous_health_maintenance"] == "supported"
    assert capabilities["capability_status"]["autonomous_memory_recovery"] == "supported"
    assert capabilities["capability_status"]["managed_compression_jobs"] == "supported"
    assert capabilities["capability_status"]["empirical_metric_calibration"] == "supported"
    assert capabilities["capability_status"]["retrieval_backend_health"] == "supported"
    assert capabilities["capability_status"]["retrieval_degradation_evidence"] == "supported"
    assert capabilities["capability_status"]["memory_os_readiness_audit"] == "supported"
