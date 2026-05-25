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
    report = MemoryOperationsEngine(gateway).recover_store(
        dry_run=False,
        layers=["project"],
    )
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


def test_memory_operations_cli_and_capabilities(
    operations_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI and provider metadata expose operational Memory OS surfaces."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(operations_repo))
    runner = CliRunner()

    metrics_result = runner.invoke(cli, ["memory-metrics", "--json"])
    capabilities_result = runner.invoke(cli, ["capabilities", "--json"])

    assert metrics_result.exit_code == 0, metrics_result.output
    assert json.loads(metrics_result.output)["status"] == "ok"
    assert capabilities_result.exit_code == 0, capabilities_result.output
    capabilities = json.loads(capabilities_result.output)
    assert capabilities["capability_status"]["lifecycle_execution"] == "supported"
    assert capabilities["capability_status"]["operational_metrics"] == "supported"
    assert capabilities["capability_status"]["memory_recovery"] == "supported"
