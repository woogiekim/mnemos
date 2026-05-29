"""Tests for the long-running beta validation harness (issue #82).

The harness drives a REAL :class:`~core.gateway.MemoryGateway` / store on an
isolated tmp home (never mocked). These tests assert the five acceptance
criteria are computed correctly, that two same-seed runs produce byte-identical
normalized reports, and that the ``mnemos beta-run`` CLI command works.

The autouse ``isolate_home`` + ``isolate_mnemos_repo_root`` fixtures
(tests/conftest.py) guarantee no test reads the developer's real store.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from core.beta_harness import (
    _CAPTURE_LAYER,
    BetaReport,
    ContinuityMetric,
    LifecycleMetric,
    RecoveryMetric,
    RelevanceMetric,
    VirtualClock,
    _build_repo,
    _build_relevance_metric,
    _probe_target_rank,
    _scan_lifecycle_violations,
    run_beta_validation,
)
from core.cli import cli
from core.gateway import MemoryGateway


# ---------------------------------------------------------------------------
# VirtualClock
# ---------------------------------------------------------------------------


def test_virtual_clock_advance_and_iso():
    """advance(days/hours) shifts now() deterministically; iso() ends with Z."""
    clock = VirtualClock(start_epoch=1_700_000_000.0, seed=42)
    assert clock.seed == 42
    assert clock.start_epoch == 1_700_000_000.0

    start = clock.now()
    clock.advance(days=1, hours=2)
    later = clock.now()

    delta = later - start
    assert delta.total_seconds() == pytest.approx(86400 + 2 * 3600)

    iso = clock.iso()
    assert iso.endswith("Z")
    # The simulated instant is timezone-aware UTC.
    assert later.tzinfo is not None


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_byte_identical(tmp_path):
    """Two same-seed runs produce byte-identical NORMALIZED JSON reports."""
    a = run_beta_validation(days=10, seed=42, home=tmp_path / "a")
    b = run_beta_validation(days=10, seed=42, home=tmp_path / "b")

    assert a.to_json() == b.to_json()


def test_different_seed_differs(tmp_path):
    """A different seed yields a different (still deterministic) run."""
    a = run_beta_validation(days=10, seed=42, home=tmp_path / "a")
    c = run_beta_validation(days=10, seed=7, home=tmp_path / "c")

    # The seed is recorded and the captures differ across seeds.
    assert a.seed != c.seed
    assert a.to_json() != c.to_json()


# ---------------------------------------------------------------------------
# AC1 / smoke
# ---------------------------------------------------------------------------


def test_runs_minimum_one_day(tmp_path):
    """The harness runs with the minimum of one simulated day."""
    report = run_beta_validation(days=1, seed=1, home=tmp_path / "one")

    assert report.days == 1
    assert report.lifecycle.days_scanned == 1
    assert report.relevance.samples == 1


def test_run_smoke_14_days(tmp_path):
    """A representative 14-day run produces a fully-populated report."""
    report = run_beta_validation(days=14, seed=42, home=tmp_path / "smoke")

    assert isinstance(report, BetaReport)
    assert report.days == 14
    assert report.total_captures > 0


def test_rejects_zero_days(tmp_path):
    """days < 1 is rejected with a ValueError."""
    with pytest.raises(ValueError):
        run_beta_validation(days=0, seed=1, home=tmp_path / "zero")


# ---------------------------------------------------------------------------
# AC2 — contextual continuity
# ---------------------------------------------------------------------------


def test_ac2_continuity_recall_full(tmp_path):
    """All fixed anchors captured early remain retrievable on a later day."""
    report = run_beta_validation(days=12, seed=42, home=tmp_path / "ac2")

    c = report.continuity
    assert isinstance(c, ContinuityMetric)
    assert c.total_anchors == 4
    assert c.surfaced_anchors == 4
    assert c.recall == 1.0


# ---------------------------------------------------------------------------
# AC3 — retrieval relevance stability
# ---------------------------------------------------------------------------


def test_ac3_relevance_stability(tmp_path):
    """The fixed probe target's rank stays stable as the store grows."""
    report = run_beta_validation(days=12, seed=42, home=tmp_path / "ac3")

    r = report.relevance
    assert isinstance(r, RelevanceMetric)
    assert r.samples == 12
    assert 0.0 <= r.normalized_rank_variance <= 1.0
    assert r.stability == pytest.approx(1.0 - r.normalized_rank_variance)
    # A stable target yields high stability.
    assert r.stability >= 0.9


def test_relevance_metric_empty_ranks():
    """The empty-ranks branch returns a defined, maximally-stable metric."""
    metric = _build_relevance_metric([])

    assert metric.samples == 0
    assert metric.observed_ranks == ()
    assert metric.rank_variance == 0.0
    assert metric.normalized_rank_variance == 0.0
    assert metric.stability == 1.0


def test_relevance_metric_variance_path():
    """Non-zero variance produces a normalized value in [0, 1] and a stability."""
    metric = _build_relevance_metric([1, 3, 2, 4])

    assert metric.samples == 4
    assert metric.rank_variance > 0.0
    assert 0.0 < metric.normalized_rank_variance <= 1.0
    assert metric.stability == pytest.approx(1.0 - metric.normalized_rank_variance)


# ---------------------------------------------------------------------------
# AC4 — lifecycle-invariant consistency
# ---------------------------------------------------------------------------


def test_ac4_lifecycle_violations_zero(tmp_path):
    """A healthy multi-day run never violates lifecycle invariants."""
    report = run_beta_validation(days=12, seed=42, home=tmp_path / "ac4")

    lc = report.lifecycle
    assert isinstance(lc, LifecycleMetric)
    assert lc.invalid_stage_count == 0
    assert lc.orphaned_supersede_count == 0
    assert lc.layer_monotonicity_violation_count == 0
    assert lc.violations == 0
    assert lc.items_scanned > 0


# ---------------------------------------------------------------------------
# AC5 — degradation + recovery
# ---------------------------------------------------------------------------


def test_ac5_degradation_detected_and_recovered(tmp_path):
    """A fault is injected, detected, and recovery restores the pre-fault state."""
    report = run_beta_validation(days=8, seed=42, home=tmp_path / "ac5")

    rc = report.recovery
    assert isinstance(rc, RecoveryMetric)
    assert rc.pre_fault_item_count > 0
    assert rc.post_fault_item_count < rc.pre_fault_item_count
    assert rc.degradation_detected is True
    assert rc.recovery_consistent is True
    assert rc.post_recovery_item_count == rc.pre_fault_item_count


# ---------------------------------------------------------------------------
# Report renderers
# ---------------------------------------------------------------------------


def test_report_renderers(tmp_path):
    """JSON is sorted/parseable and the markdown summary carries an H1 + ACs."""
    report = run_beta_validation(days=6, seed=42, home=tmp_path / "render")

    parsed = json.loads(report.to_json())
    assert parsed["seed"] == 42
    assert parsed["days"] == 6
    assert set(report.to_dict()) >= {
        "continuity",
        "relevance",
        "lifecycle",
        "recovery",
    }

    md = report.to_markdown()
    assert md.startswith("# Beta Validation Report")
    assert "AC2 — Contextual continuity" in md
    assert "AC5 — Degradation + recovery" in md


# ---------------------------------------------------------------------------
# Lifecycle-scan violation branches (white-box)
# ---------------------------------------------------------------------------


def test_scan_detects_each_violation_kind(tmp_path):
    """The lifecycle scan counts invalid stage, orphaned supersede, and bad layer."""
    repo_root = _build_repo(tmp_path / "scan")
    gateway = MemoryGateway(repo_root=str(repo_root))

    # Healthy item — no violations.
    gateway.capture(
        content="healthy lifecycle item",
        layer=_CAPTURE_LAYER,
        run_id="scan",
        no_classify=True,
    )

    # Violation item: an invalid stage, an orphaned superseded_by pointer, and
    # an unknown layer (so get_next_layer raises).
    bad_id = gateway.capture(
        content="violating lifecycle item",
        layer=_CAPTURE_LAYER,
        run_id="scan",
        no_classify=True,
    )
    bad = gateway._store.read(bad_id)
    gateway._store.update(
        bad["_path"],
        metadata_updates={
            "stage": "not-a-real-stage",
            "superseded_by": "missing-item-id",
            "layer": "no-such-layer",
        },
    )

    scanned, invalid_stage, orphaned, mono = _scan_lifecycle_violations(gateway)

    assert scanned >= 2
    assert invalid_stage == 1
    assert orphaned == 1
    assert mono == 1


def test_probe_target_rank_absent_returns_zero(tmp_path):
    """Probing for an id that is not in the result list returns rank 0."""
    repo_root = _build_repo(tmp_path / "probe")
    gateway = MemoryGateway(repo_root=str(repo_root))

    gateway.capture(
        content="some unrelated note",
        layer=_CAPTURE_LAYER,
        run_id="probe",
        no_classify=True,
    )

    assert _probe_target_rank(gateway, "definitely-not-present") == 0


# ---------------------------------------------------------------------------
# CLI — mnemos beta-run
# ---------------------------------------------------------------------------


def test_cli_beta_run_text():
    """Default text output emits the markdown report."""
    result = CliRunner().invoke(cli, ["beta-run", "--days", "4", "--seed", "5"])

    assert result.exit_code == 0
    assert "# Beta Validation Report" in result.output


def test_cli_beta_run_json():
    """--json emits a parseable JSON report."""
    result = CliRunner().invoke(cli, ["beta-run", "--days", "4", "--seed", "42", "--json"])

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["seed"] == 42


def test_cli_beta_run_output_file(tmp_path):
    """--output writes the report to the given path and prints a notice."""
    out = tmp_path / "report.json"
    result = CliRunner().invoke(
        cli,
        ["beta-run", "--days", "5", "--seed", "42", "--json", "--output", str(out)],
    )

    assert result.exit_code == 0
    assert "report written" in result.output
    assert out.exists()
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["days"] == 5


def test_cli_beta_run_error_exit(monkeypatch):
    """A harness error surfaces as a non-zero CLI exit with an error message."""

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated harness failure")

    monkeypatch.setattr("core.beta_harness.run_beta_validation", _boom)

    result = CliRunner().invoke(cli, ["beta-run", "--days", "3", "--seed", "1"])

    assert result.exit_code == 1
    assert "error:" in result.output
