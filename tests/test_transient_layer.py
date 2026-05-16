"""TDD tests for the transient layer — mnemos #20 gap 1.

The transient layer is designed for protocol-test and throwaway captures
(e.g. "test capture for protocol verification", "테스트 캡처") that must
NOT pollute the session layer.  Key requirements:

- Layer name: "transient"
- Storage path: .agent/transient/ (flat, no run_id/session_id)
- GC staleness window: 1 hour (much shorter than session's 24 h)
- NOT a promotion source — transient items are NOT promoted automatically
- Must be a valid layer in policy.yaml (validate_capture passes)
- GarbageCollector includes transient in its scan and applies 1h staleness
- CLI `mnemos capture --layer transient` works end-to-end
"""
from __future__ import annotations

import datetime
import yaml
from pathlib import Path

import pytest

from core.gc import (
    GarbageCollector,
    ALL_LAYERS,
    TRANSIENT_STALENESS_HOURS,
    compute_garbage_score,
)
from core.layers import LAYER_STATIC_PATHS, TRANSIENT_PATH
from core.store import MemoryStore


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_transient_memory(
    repo_root: Path,
    *,
    item_id: str = "tx-test",
    stage: str = "stored",
    quality_score: float = 0.8,
    access_count: int = 0,
    created_at: str | None = None,
    content: str = "test capture for protocol verification",
) -> Path:
    """Write a memory file into the transient layer directory."""
    if created_at is None:
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    transient_dir = repo_root / ".agent" / "transient"
    transient_dir.mkdir(parents=True, exist_ok=True)
    file_path = transient_dir / f"{item_id}.md"
    front_matter = (
        f"---\n"
        f"id: {item_id}\n"
        f"layer: transient\n"
        f"stage: {stage}\n"
        f"quality_score: {quality_score}\n"
        f"access_count: {access_count}\n"
        f"created_at: '{created_at}'\n"
        f"---\n"
        f"{content}\n"
    )
    file_path.write_text(front_matter)
    return file_path


def _make_repo(tmp_path: Path, *, include_transient: bool = True) -> Path:
    """Create a minimal mnemos repo with policy.yaml including transient."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)
    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "transient"]:
        (agent / d).mkdir(parents=True)

    layers: dict = {
        "ephemeral": {
            "path_template": ".agent/runs/{run_id}/scratch/",
            "promotes_to": "working",
            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
        },
        "working": {
            "path_template": ".agent/runs/{run_id}/working/",
            "promotes_to": "session",
            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
        },
        "session": {
            "path_template": ".agent/sessions/{session_id}/",
            "promotes_to": "project",
            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
        },
        "project": {
            "path_template": "wiki/projects/",
            "promotes_to": "global",
            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
        },
        "global": {
            "path_template": "wiki/global/",
            "promotes_to": None,
            "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
        },
    }
    if include_transient:
        layers["transient"] = {
            "path_template": ".agent/transient/",
            "promotes_to": None,
            "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
        }

    policy = {
        "layers": layers,
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


# ---------------------------------------------------------------------------
# Test: layers.py exposes TRANSIENT_PATH constant
# ---------------------------------------------------------------------------

class TestLayersModule:
    def test_transient_path_constant_defined(self):
        """TRANSIENT_PATH must be importable from core.layers."""
        assert TRANSIENT_PATH is not None
        assert isinstance(TRANSIENT_PATH, str)
        # Must point to .agent/transient (relative)
        assert "transient" in TRANSIENT_PATH

    def test_transient_not_in_layer_static_paths(self):
        """Transient is dynamic (not in LAYER_STATIC_PATHS) — it has its own
        path resolution in the store, not the static wiki/ mapping."""
        assert "transient" not in LAYER_STATIC_PATHS


# ---------------------------------------------------------------------------
# Test: store.py handles transient layer path resolution
# ---------------------------------------------------------------------------

class TestStoreTransientLayer:
    def test_layer_path_returns_agent_transient(self, tmp_path):
        """MemoryStore._layer_path("transient") must return .agent/transient."""
        store = MemoryStore(repo_root=str(tmp_path))
        path = store._layer_path("transient")
        assert path == tmp_path / ".agent" / "transient"

    def test_write_and_read_transient(self, tmp_path):
        """Round-trip write/read for transient layer."""
        (tmp_path / ".agent" / "transient").mkdir(parents=True)
        store = MemoryStore(repo_root=str(tmp_path))
        written = store.write(
            layer="transient",
            item_id="tx-roundtrip",
            content="protocol test capture",
            metadata={
                "id": "tx-roundtrip",
                "layer": "transient",
                "stage": "stored",
                "created_at": "2026-05-17T00:00:00Z",
                "access_count": 0,
                "quality_score": 0.5,
            },
        )
        assert written.exists()
        parsed = store._parse_file(written)
        assert parsed["layer"] == "transient"
        assert "protocol test capture" in parsed["content"]

    def test_write_transient_does_not_need_run_id(self, tmp_path):
        """Transient write must not require run_id — it uses a flat dir."""
        (tmp_path / ".agent" / "transient").mkdir(parents=True)
        store = MemoryStore(repo_root=str(tmp_path))
        # No run_id passed — must not raise
        written = store.write(
            layer="transient",
            item_id="tx-no-run-id",
            content="throwaway",
            metadata={"id": "tx-no-run-id", "layer": "transient", "stage": "stored",
                      "created_at": "2026-05-17T00:00:00Z", "access_count": 0,
                      "quality_score": 0.5},
        )
        assert written.exists()


# ---------------------------------------------------------------------------
# Test: GC treats transient with 1 h staleness
# ---------------------------------------------------------------------------

class TestGCTransientLayer:
    def test_transient_in_all_layers(self):
        """ALL_LAYERS must include 'transient'."""
        assert "transient" in ALL_LAYERS

    def test_transient_staleness_constant(self):
        """TRANSIENT_STALENESS_HOURS must be <= 1.0 (short expiry)."""
        assert TRANSIENT_STALENESS_HOURS <= 1.0
        assert TRANSIENT_STALENESS_HOURS > 0.0

    def test_old_transient_item_gets_high_gc_score(self):
        """A transient item created 2h ago must score high under 1h staleness."""
        now = datetime.datetime.now(datetime.timezone.utc)
        item = {
            "layer": "transient",
            "created_at": (now - datetime.timedelta(hours=2)).isoformat(),
            "last_access_at": None,
            "access_count": 0,
            "quality_score": 0.8,
            "stage": "stored",
        }
        score, _ = compute_garbage_score(
            item, staleness_hours=TRANSIENT_STALENESS_HOURS, now=now
        )
        # With 2h old and 1h window, staleness saturates → high score
        assert score >= 0.5

    def test_fresh_transient_item_scores_lower(self):
        """A transient item created 2 min ago must score lower than an old one."""
        now = datetime.datetime.now(datetime.timezone.utc)
        old_item = {
            "layer": "transient",
            "created_at": (now - datetime.timedelta(hours=2)).isoformat(),
            "access_count": 0,
            "quality_score": 0.8,
            "stage": "stored",
        }
        fresh_item = {
            "layer": "transient",
            "created_at": (now - datetime.timedelta(minutes=2)).isoformat(),
            "access_count": 0,
            "quality_score": 0.8,
            "stage": "stored",
        }
        old_score, _ = compute_garbage_score(
            old_item, staleness_hours=TRANSIENT_STALENESS_HOURS, now=now
        )
        fresh_score, _ = compute_garbage_score(
            fresh_item, staleness_hours=TRANSIENT_STALENESS_HOURS, now=now
        )
        assert old_score > fresh_score

    def test_gc_scans_transient_region(self, tmp_path):
        """GarbageCollector.scan() must return a region for 'transient'."""
        repo = _make_repo(tmp_path)
        gc = GarbageCollector(repo_root=str(repo))
        regions = gc.scan()
        layer_names = [r.layer for r in regions]
        assert "transient" in layer_names

    def test_gc_collects_old_transient(self, tmp_path):
        """GarbageCollector.run(dry_run=True) must identify old transient as collectable."""
        repo = _make_repo(tmp_path)
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=3)).isoformat()
        _write_transient_memory(repo, item_id="old-tx", created_at=old_ts,
                                access_count=0)
        gc = GarbageCollector(
            repo_root=str(repo),
            staleness_hours=TRANSIENT_STALENESS_HOURS,
            gc_threshold=0.3,
        )
        report = gc.run(dry_run=True)
        archived_ids = [i["item_id"] for i in report.archived_items]
        assert "old-tx" in archived_ids

    def test_gc_does_not_collect_fresh_transient(self, tmp_path):
        """GarbageCollector.run() must NOT collect a transient created moments ago."""
        repo = _make_repo(tmp_path)
        _write_transient_memory(repo, item_id="fresh-tx")
        gc = GarbageCollector(
            repo_root=str(repo),
            staleness_hours=TRANSIENT_STALENESS_HOURS,
            gc_threshold=0.7,
        )
        report = gc.run(dry_run=True)
        archived_ids = [i["item_id"] for i in report.archived_items]
        assert "fresh-tx" not in archived_ids


# ---------------------------------------------------------------------------
# Test: CLI capture --layer transient
# ---------------------------------------------------------------------------

class TestCLITransientCapture:
    def test_capture_transient_layer_succeeds(self, tmp_path):
        """mnemos capture --layer transient must exit 0 and emit captured ID."""
        import os
        from click.testing import CliRunner
        from core.cli import cli

        repo = _make_repo(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["capture", "--layer", "transient", "--content",
             "test capture for protocol verification"],
            env={"MNEMOS_REPO_ROOT": str(repo)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower()

    def test_capture_transient_stored_in_agent_transient_dir(self, tmp_path):
        """Files captured with --layer transient must land in .agent/transient/."""
        from click.testing import CliRunner
        from core.cli import cli

        repo = _make_repo(tmp_path)
        runner = CliRunner()
        runner.invoke(
            cli,
            ["capture", "--layer", "transient", "--content", "throwaway test"],
            env={"MNEMOS_REPO_ROOT": str(repo)},
            catch_exceptions=False,
        )
        transient_dir = repo / ".agent" / "transient"
        files = list(transient_dir.glob("*.md"))
        assert len(files) >= 1


# ---------------------------------------------------------------------------
# Test: policy.yaml includes transient (integration)
# ---------------------------------------------------------------------------

class TestPolicyTransient:
    def test_policy_validate_capture_accepts_transient(self, tmp_path):
        """PolicyEngine.validate_capture must accept 'transient' after policy update."""
        from core.policy import PolicyEngine

        repo = _make_repo(tmp_path, include_transient=True)
        engine = PolicyEngine(policy_path=str(repo / "wiki" / "policy.yaml"))
        # Must not raise
        engine.validate_capture(layer="transient", item={"content": "test"})

    def test_policy_rejects_transient_promotion(self, tmp_path):
        """Transient promotes_to=null — validate_promote must reject promotion attempt."""
        from core.policy import PolicyEngine, PolicyViolationError

        repo = _make_repo(tmp_path, include_transient=True)
        engine = PolicyEngine(policy_path=str(repo / "wiki" / "policy.yaml"))
        item = {
            "layer": "transient",
            "quality_score": 0.9,
            "access_count": 10,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with pytest.raises(PolicyViolationError):
            engine.validate_promote(item=item, target_layer="session")
