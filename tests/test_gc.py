"""Tests for the G1GC-style garbage collector (core/gc.py)."""
from __future__ import annotations

import datetime
import yaml
from pathlib import Path

import pytest

from core.gc import (
    GarbageCollector,
    GCReport,
    GCRegion,
    MemoryRecord,
    compute_garbage_score,
    _staleness_score,
    _access_score,
    _quality_score,
    _stage_score,
    ALL_LAYERS,
    DEFAULT_GC_THRESHOLD,
    DEFAULT_STALENESS_HOURS,
    DEFAULT_LIMIT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal mnemos repo structure and return its path."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state"]:
        (agent / d).mkdir(parents=True)

    policy = {
        "layers": {
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
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


def _write_memory(
    path: Path,
    *,
    item_id: str = "test-item",
    layer: str = "ephemeral",
    stage: str = "stored",
    quality_score: float = 0.8,
    access_count: int = 0,
    created_at: str | None = None,
    content: str = "Test memory content",
) -> Path:
    """Write a minimal memory file with YAML front-matter."""
    if created_at is None:
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    front_matter = (
        f"---\n"
        f"id: {item_id}\n"
        f"layer: {layer}\n"
        f"stage: {stage}\n"
        f"quality_score: {quality_score}\n"
        f"access_count: {access_count}\n"
        f"created_at: '{created_at}'\n"
        f"---\n"
        f"{content}\n"
    )
    path.write_text(front_matter)
    return path


# ---------------------------------------------------------------------------
# Unit tests: scoring functions
# ---------------------------------------------------------------------------

class TestStalenessScore:
    def test_no_timestamp_returns_max(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        score = _staleness_score(None, None, staleness_hours=24.0, now=now)
        assert score == 1.0

    def test_fresh_item_scores_low(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        created = now - datetime.timedelta(minutes=5)
        score = _staleness_score(created, None, staleness_hours=24.0, now=now)
        assert score < 0.1

    def test_old_item_scores_high(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        created = now - datetime.timedelta(hours=96)
        score = _staleness_score(created, None, staleness_hours=24.0, now=now)
        assert score > 0.9

    def test_recently_accessed_item_is_not_stale(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        created = now - datetime.timedelta(hours=72)
        last_access = now - datetime.timedelta(minutes=10)
        score = _staleness_score(created, last_access, staleness_hours=24.0, now=now)
        # last_access is only 10 min ago → should be very low
        assert score < 0.1

    def test_zero_staleness_hours_returns_zero(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        created = now - datetime.timedelta(hours=100)
        score = _staleness_score(created, None, staleness_hours=0.0, now=now)
        assert score == 0.0


class TestAccessScore:
    def test_zero_access_scores_high(self):
        assert _access_score(0, "ephemeral") > 0.5

    def test_above_baseline_scores_zero(self):
        # ephemeral baseline=1; 3 accesses is above baseline
        assert _access_score(3, "ephemeral") == 0.0

    def test_project_layer_higher_baseline(self):
        # project baseline=5; 1 access still has high garbage potential
        assert _access_score(1, "project") > 0.5

    def test_all_layers_produce_score_in_range(self):
        from core.gc import ALL_LAYERS
        for layer in ALL_LAYERS:
            for count in [0, 1, 5, 10]:
                s = _access_score(count, layer)
                assert 0.0 <= s <= 1.0, f"Out of range for layer={layer} count={count}"


class TestQualityScore:
    def test_perfect_quality_scores_zero(self):
        assert _quality_score(1.0) == 0.0

    def test_zero_quality_scores_one(self):
        assert _quality_score(0.0) == 1.0

    def test_midpoint_quality(self):
        score = _quality_score(0.5)
        assert abs(score - 0.5) < 1e-9

    def test_range(self):
        for q in [0.0, 0.3, 0.5, 0.7, 1.0]:
            s = _quality_score(q)
            assert 0.0 <= s <= 1.0


class TestStageScore:
    def test_archived_is_maximum(self):
        assert _stage_score("archived") == 1.0

    def test_active_stages_are_low(self):
        for stage in ("retrieved", "used", "validated", "classified", "promoted"):
            assert _stage_score(stage) <= 0.2

    def test_unknown_stage_is_nonzero(self):
        assert _stage_score("unknown_xyz") > 0.0


class TestComputeGarbageScore:
    def _make_item(self, **kwargs) -> dict:
        now = datetime.datetime.now(datetime.timezone.utc)
        defaults = {
            "layer": "ephemeral",
            "stage": "stored",
            "quality_score": 0.8,
            "access_count": 0,
            "created_at": (now - datetime.timedelta(hours=48)).isoformat(),
        }
        defaults.update(kwargs)
        return defaults

    def test_returns_tuple_of_float_and_dict(self):
        item = self._make_item()
        score, breakdown = compute_garbage_score(item)
        assert isinstance(score, float)
        assert isinstance(breakdown, dict)
        assert set(breakdown.keys()) == {"staleness", "access", "quality", "stage"}

    def test_score_in_range(self):
        item = self._make_item()
        score, _ = compute_garbage_score(item)
        assert 0.0 <= score <= 1.0

    def test_garbage_item_scores_high(self):
        """Old, never-accessed, low-quality, archived item → high score."""
        now = datetime.datetime.now(datetime.timezone.utc)
        item = self._make_item(
            created_at=(now - datetime.timedelta(hours=200)).isoformat(),
            access_count=0,
            quality_score=0.1,
            stage="archived",
        )
        score, _ = compute_garbage_score(item, staleness_hours=24.0, now=now)
        assert score >= 0.8

    def test_healthy_item_scores_low(self):
        """Fresh, frequently-accessed, high-quality item → low score."""
        now = datetime.datetime.now(datetime.timezone.utc)
        item = self._make_item(
            created_at=(now - datetime.timedelta(minutes=30)).isoformat(),
            access_count=10,
            quality_score=0.95,
            stage="used",
        )
        score, _ = compute_garbage_score(item, staleness_hours=24.0, now=now)
        assert score < 0.3

    def test_breakdown_sums_to_score(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        item = self._make_item()
        score, breakdown = compute_garbage_score(item, now=now)
        assert abs(sum(breakdown.values()) - score) < 1e-3

    def test_custom_staleness_hours(self):
        """Higher staleness_hours → same-age item looks less stale."""
        now = datetime.datetime.now(datetime.timezone.utc)
        item = self._make_item(
            created_at=(now - datetime.timedelta(hours=12)).isoformat(),
            access_count=0,
            quality_score=0.5,
        )
        score_strict, _ = compute_garbage_score(item, staleness_hours=6.0, now=now)
        score_lenient, _ = compute_garbage_score(item, staleness_hours=48.0, now=now)
        assert score_strict > score_lenient


# ---------------------------------------------------------------------------
# Integration tests: GarbageCollector
# ---------------------------------------------------------------------------

class TestGarbageCollector:
    def _gc(
        self,
        repo_root: Path,
        *,
        threshold: float = 0.0,  # zero = collect everything
        staleness_hours: float = 0.01,  # tiny → old immediately
        limit: int = 100,
        layers: list[str] | None = None,
    ) -> GarbageCollector:
        return GarbageCollector(
            repo_root=str(repo_root),
            staleness_hours=staleness_hours,
            gc_threshold=threshold,
            limit=limit,
            layers=layers,
        )

    def test_empty_repo_returns_empty_report(self, repo_root: Path):
        gc = self._gc(repo_root)
        report = gc.run(dry_run=True)
        assert isinstance(report, GCReport)
        assert report.scanned == 0
        assert report.archived == 0

    def test_scan_returns_regions_in_all_layers(self, repo_root: Path):
        gc = self._gc(repo_root)
        regions = gc.scan()
        layer_names = {r.layer for r in regions}
        # Should contain at least all configured layers
        assert layer_names == set(ALL_LAYERS)

    def test_regions_ordered_by_total_garbage_score(self, repo_root: Path):
        """G1GC property: regions with higher total score come first."""
        # Write a very old file into ephemeral
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "old-item.md",
            item_id="old-item",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        gc = self._gc(repo_root)
        regions = gc.scan()
        non_empty = [r for r in regions if r.records]
        assert non_empty, "Expected at least one non-empty region"
        # Ephemeral should have the highest total score
        assert non_empty[0].layer == "ephemeral"

    def test_dry_run_does_not_modify_files(self, repo_root: Path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        path = scratch / "test-item.md"
        _write_memory(
            path,
            item_id="test-item",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )
        original_content = path.read_text()

        gc = self._gc(repo_root, threshold=0.0)
        report = gc.run(dry_run=True)

        assert report.dry_run is True
        assert report.archived > 0
        # File must not have been modified
        assert path.read_text() == original_content

    def test_live_run_archives_high_score_items(self, repo_root: Path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        path = scratch / "target.md"
        _write_memory(
            path,
            item_id="target",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        gc = self._gc(repo_root, threshold=0.0)
        report = gc.run(dry_run=False)

        assert report.archived >= 1
        # The file should now have stage=archived in its front-matter
        content = path.read_text()
        assert "stage: archived" in content

    def test_threshold_filters_low_score_items(self, repo_root: Path):
        """Items with score below threshold should not be archived."""
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh_ts = (now - datetime.timedelta(minutes=1)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "fresh.md",
            item_id="fresh",
            layer="ephemeral",
            stage="used",
            quality_score=0.95,
            access_count=10,
            created_at=fresh_ts,
        )

        gc = self._gc(repo_root, threshold=0.9)
        report = gc.run(dry_run=False)

        # The fresh item should NOT be archived
        assert all(i["item_id"] != "fresh" for i in report.archived_items)

    def test_limit_caps_archived_count(self, repo_root: Path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        for i in range(10):
            _write_memory(
                scratch / f"item-{i}.md",
                item_id=f"item-{i}",
                layer="ephemeral",
                stage="stored",
                quality_score=0.1,
                access_count=0,
                created_at=old_ts,
            )

        gc = self._gc(repo_root, threshold=0.0, limit=3)
        report = gc.run(dry_run=False)

        assert report.archived <= 3

    def test_layer_filter_restricts_to_specified_layers(self, repo_root: Path):
        """When --layer=project, only project items should be scanned."""
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        # Write into ephemeral (should be ignored)
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "ephemeral-item.md",
            item_id="ephemeral-item",
            layer="ephemeral",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )
        # Write into project layer
        proj = repo_root / "wiki" / "projects"
        _write_memory(
            proj / "proj-item.md",
            item_id="proj-item",
            layer="project",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        gc = GarbageCollector(
            repo_root=str(repo_root),
            staleness_hours=0.01,
            gc_threshold=0.0,
            limit=100,
            layers=["project"],
        )
        report = gc.run(dry_run=True)

        item_ids = [i["item_id"] for i in report.archived_items]
        assert "proj-item" in item_ids
        assert "ephemeral-item" not in item_ids

    def test_report_summary_lines_dry_run(self, repo_root: Path):
        gc = self._gc(repo_root)
        report = gc.run(dry_run=True)
        lines = report.summary_lines()
        assert any("[dry-run]" in line for line in lines)

    def test_report_summary_lines_live(self, repo_root: Path):
        gc = self._gc(repo_root)
        report = gc.run(dry_run=False)
        lines = report.summary_lines()
        assert not any("[dry-run]" in line for line in lines)

    def test_session_layer_items_scanned(self, repo_root: Path):
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        session_dir = repo_root / ".agent" / "sessions" / "sess-1"
        _write_memory(
            session_dir / "session-item.md",
            item_id="session-item",
            layer="session",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        gc = GarbageCollector(
            repo_root=str(repo_root),
            staleness_hours=0.01,
            gc_threshold=0.0,
            limit=100,
            layers=["session"],
        )
        report = gc.run(dry_run=True)
        assert report.scanned >= 1
        item_ids = [i["item_id"] for i in report.archived_items]
        assert "session-item" in item_ids


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestGCCliCommand:
    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    def test_gc_dry_run_flag(self, runner, repo_root: Path, monkeypatch):
        """mnemos gc --dry-run should print '[dry-run]' and not modify files."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli

        result = runner.invoke(cli, ["gc", "--dry-run"])
        assert result.exit_code == 0
        assert "[dry-run]" in result.output

    def test_gc_empty_repo(self, runner, repo_root: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli

        result = runner.invoke(cli, ["gc"])
        assert result.exit_code == 0
        assert "Scanned  : 0" in result.output

    def test_gc_layer_option(self, runner, repo_root: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli

        result = runner.invoke(cli, ["gc", "--layer", "ephemeral,working", "--dry-run"])
        assert result.exit_code == 0

    def test_gc_threshold_option(self, runner, repo_root: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli

        result = runner.invoke(cli, ["gc", "--threshold", "0.5", "--dry-run"])
        assert result.exit_code == 0

    def test_gc_verbose_option(self, runner, repo_root: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli

        result = runner.invoke(cli, ["gc", "--dry-run", "--verbose"])
        assert result.exit_code == 0

    def test_gc_archives_item_in_live_run(self, runner, repo_root: Path, monkeypatch):
        """Live run should set stage=archived in the file."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))

        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        path = scratch / "gc-target.md"
        _write_memory(
            path,
            item_id="gc-target",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        from core.cli import cli
        result = runner.invoke(
            cli,
            ["gc", "--threshold", "0.0", "--staleness-hours", "0.01"],
        )
        assert result.exit_code == 0
        content = path.read_text()
        assert "stage: archived" in content


# ---------------------------------------------------------------------------
# Gateway API tests
# ---------------------------------------------------------------------------

class TestGatewayGCMethod:
    def test_gateway_gc_returns_report(self, repo_root: Path):
        from core.gateway import MemoryGateway
        from core.gc import GCReport

        gw = MemoryGateway(repo_root=str(repo_root))
        report = gw.gc(dry_run=True)
        assert isinstance(report, GCReport)

    def test_gateway_gc_dry_run_no_mutation(self, repo_root: Path):
        from core.gateway import MemoryGateway

        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        path = scratch / "gw-item.md"
        _write_memory(
            path,
            item_id="gw-item",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )
        original = path.read_text()

        gw = MemoryGateway(repo_root=str(repo_root))
        gw.gc(dry_run=True, gc_threshold=0.0, staleness_hours=0.01)

        assert path.read_text() == original

    def test_gateway_gc_live_logs_audit(self, repo_root: Path):
        from core.gateway import MemoryGateway

        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "audit-item.md",
            item_id="audit-item",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )

        gw = MemoryGateway(repo_root=str(repo_root))
        report = gw.gc(dry_run=False, gc_threshold=0.0, staleness_hours=0.01)

        if report.archived > 0:
            log_path = repo_root / "wiki" / "log.jsonl"
            log_content = log_path.read_text()
            assert "gc_archive" in log_content
