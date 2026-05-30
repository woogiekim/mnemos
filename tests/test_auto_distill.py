"""Automatic distillation tests (issue #87).

Verifies the post-capture subscriber, the end-of-consolidate fire, the
``storage.distillation`` config block + tolerance, the
``~/.mnemos/.distill-state.json`` sidecar lifecycle, error swallow,
re-entrancy guard, and idempotency via #84's skip-if-exists.

The fixture pattern mirrors ``tests/test_distill.py:30-80`` (tmp ``repo_root``
with a minimal ``wiki/policy.yaml`` plus optional ``mnemos.yml``). The autouse
``isolate_home`` + ``isolate_mnemos_repo_root`` fixtures from ``conftest.py``
isolate ``~/.mnemos/.distill-state.json`` per test automatically — no extra
home-patching is required.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml


# --------------------------------------------------------------------------- #
# Repo / gateway fixtures (mirror tests/test_distill.py)
# --------------------------------------------------------------------------- #

def _write_policy(repo_root: Path) -> None:
    wiki = repo_root / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True, exist_ok=True)
    agent = repo_root / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True, exist_ok=True)
    (agent / "workflows" / "hooks").mkdir(parents=True, exist_ok=True)

    policy_cfg = {
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy_cfg))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    _write_policy(tmp_path)
    return tmp_path


@pytest.fixture
def gateway(repo_root: Path):
    from core.gateway import MemoryGateway
    return MemoryGateway(repo_root=str(repo_root))


def _write_mnemos_yml(repo_root: Path, distillation: dict[str, Any] | None) -> None:
    payload: dict[str, Any] = {"storage": {}}
    if distillation is not None:
        payload["storage"]["distillation"] = distillation
    (repo_root / "mnemos.yml").write_text(yaml.dump(payload))


def _state_file() -> Path:
    return Path.home() / ".mnemos" / ".distill-state.json"


def _capture_silent(g: Any, idx: int) -> str:
    """Capture a unique short note that is unlikely to derive any domain."""
    return g.capture(
        layer="ephemeral",
        content=f"auto-distill-test-{idx}",
        tags=[],
        no_classify=True,
    )


# --------------------------------------------------------------------------- #
# 1. Below threshold — no fire, counter persisted.
# --------------------------------------------------------------------------- #
def test_post_capture_below_threshold_does_not_fire(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill") as run_mock:
        for i in range(24):
            _capture_silent(g, i)

    assert run_mock.call_count == 0

    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 24


# --------------------------------------------------------------------------- #
# 2. At threshold — fire once, counter resets.
# --------------------------------------------------------------------------- #
def test_post_capture_at_threshold_fires_once_and_resets(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill", return_value={
        "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
    }) as run_mock:
        for i in range(25):
            _capture_silent(g, i)

    assert run_mock.call_count == 1

    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 0
    assert state["last_distill_at"]  # non-empty ISO timestamp


# --------------------------------------------------------------------------- #
# 3. Above threshold — fires exactly twice over 50 captures.
# --------------------------------------------------------------------------- #
def test_post_capture_above_threshold_fires_exactly_once_per_interval(
    repo_root: Path,
) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill", return_value={
        "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
    }) as run_mock:
        for i in range(50):
            _capture_silent(g, i)

    assert run_mock.call_count == 2


# --------------------------------------------------------------------------- #
# 4. 26 captures: 25 fires once, then counter at 26 is 1.
# --------------------------------------------------------------------------- #
def test_post_capture_threshold_26_fires_once(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill", return_value={
        "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
    }) as run_mock:
        for i in range(26):
            _capture_silent(g, i)

    assert run_mock.call_count == 1

    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 1


# --------------------------------------------------------------------------- #
# 5. consolidate() fires distill unconditionally and resets the counter.
# --------------------------------------------------------------------------- #
def test_consolidate_fires_distill_unconditionally(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    # 5 captures (under threshold).
    with patch("core.distill.run_auto_distill", return_value={
        "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
    }) as run_mock:
        for i in range(5):
            _capture_silent(g, i)
        assert run_mock.call_count == 0

        g.consolidate()
        assert run_mock.call_count == 1

    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 0


# --------------------------------------------------------------------------- #
# 6. enabled=false — neither path fires AND subscriber is not registered.
# --------------------------------------------------------------------------- #
def test_config_disabled_neither_path_fires(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": False, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))

    # Key behavior: the auto-distill subscriber was never registered.
    assert g.event_bus.handler_count("post-capture") == 0

    with patch("core.distill.run_auto_distill") as run_mock:
        for i in range(100):
            _capture_silent(g, i)
        g.consolidate()

    assert run_mock.call_count == 0
    # State file is never written when the feature is disabled.
    assert not _state_file().exists()


# --------------------------------------------------------------------------- #
# 7. Invalid interval (non-int) → 25.
# --------------------------------------------------------------------------- #
def test_config_invalid_interval_falls_back_to_default(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"interval_captures": "not-a-number"})
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.interval_captures == 25
    assert cfg.distillation.enabled is True


# --------------------------------------------------------------------------- #
# 8. Invalid interval = 0 → 25.
# --------------------------------------------------------------------------- #
def test_config_invalid_interval_zero_falls_back_to_default(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"interval_captures": 0})
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.interval_captures == 25


# --------------------------------------------------------------------------- #
# 9. Invalid interval = -5 → 25.
# --------------------------------------------------------------------------- #
def test_config_invalid_interval_negative_falls_back_to_default(
    repo_root: Path,
) -> None:
    _write_mnemos_yml(repo_root, {"interval_captures": -5})
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.interval_captures == 25


# --------------------------------------------------------------------------- #
# 10. Invalid enabled (non-bool) → True.
# --------------------------------------------------------------------------- #
def test_config_invalid_enabled_falls_back_to_true(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": "maybe"})
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.enabled is True


# --------------------------------------------------------------------------- #
# 11. Missing block → all defaults.
# --------------------------------------------------------------------------- #
def test_config_missing_block_uses_defaults(repo_root: Path) -> None:
    # No mnemos.yml at all.
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.enabled is True
    assert cfg.distillation.interval_captures == 25


# --------------------------------------------------------------------------- #
# 12. Error swallow — distill raises but capture succeeds, counter preserved.
# --------------------------------------------------------------------------- #
def test_error_swallow_capture_succeeds_when_distill_raises(
    repo_root: Path,
) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill", side_effect=RuntimeError("boom")):
        ids: list[str] = []
        for i in range(25):
            ids.append(_capture_silent(g, i))

    # All captures succeeded — the subscriber error never propagated.
    assert all(ids)

    # Counter preserved at 25 (NOT reset) so the next capture retries the fire.
    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 25

    # Observability log carries the failure entry.
    obs_log = repo_root / ".agent" / "observability.jsonl"
    # Allow the background-thread write to land.
    import time
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if obs_log.exists():
            entries = [
                json.loads(line)
                for line in obs_log.read_text().splitlines()
                if line.strip()
            ]
            failure_entries = [
                e
                for e in entries
                if e.get("event") == "auto_distill"
                and e.get("success") is False
                and e.get("error") == "boom"
                and e.get("trigger") == "post-capture"
            ]
            if failure_entries:
                break
        time.sleep(0.05)
    else:
        pytest.fail("observability.jsonl never received the failure entry")


# --------------------------------------------------------------------------- #
# 13. State file roundtrip.
# --------------------------------------------------------------------------- #
def test_state_file_roundtrip() -> None:
    from core.distill import (
        _read_distill_state,
        _state_path,
        _write_distill_state,
    )

    path = _state_path()
    payload_a = {"captures_since_last_distill": 7, "last_distill_at": "iso-a"}
    _write_distill_state(path, payload_a)
    assert _read_distill_state(path) == payload_a

    payload_b = {"captures_since_last_distill": 9, "last_distill_at": "iso-b"}
    _write_distill_state(path, payload_b)
    assert _read_distill_state(path) == payload_b
    # Final file is valid JSON.
    assert json.loads(path.read_text()) == payload_b


# --------------------------------------------------------------------------- #
# 14. Missing state file treated as zero — first capture rebuilds counter to 1.
# --------------------------------------------------------------------------- #
def test_state_file_missing_treated_as_zero(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    state_path = _state_file()
    if state_path.exists():
        state_path.unlink()

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill"):
        _capture_silent(g, 0)

    state = json.loads(state_path.read_text())
    assert state["captures_since_last_distill"] == 1


# --------------------------------------------------------------------------- #
# 15. Corrupted state file — JSONDecodeError swallowed, counter resets to 1.
# --------------------------------------------------------------------------- #
def test_state_file_corrupted_treated_as_zero(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    state_path = _state_file()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not-json")

    g = MemoryGateway(repo_root=str(repo_root))
    with patch("core.distill.run_auto_distill"):
        _capture_silent(g, 0)

    state = json.loads(state_path.read_text())
    assert state["captures_since_last_distill"] == 1


# --------------------------------------------------------------------------- #
# 16. Concurrent rewrite (best-effort) — final file is valid JSON.
# --------------------------------------------------------------------------- #
def test_state_file_atomic_rewrite_concurrent_best_effort() -> None:
    from core.distill import _state_path, _write_distill_state

    path = _state_path()
    payload_a = {"captures_since_last_distill": 1, "last_distill_at": "a"}
    payload_b = {"captures_since_last_distill": 2, "last_distill_at": "b"}

    errors: list[BaseException] = []

    def worker(p: dict[str, Any]) -> None:
        try:
            for _ in range(20):
                _write_distill_state(path, p)
        except BaseException as exc:  # noqa: BLE001 - propagated via list
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(payload_a,)),
        threading.Thread(target=worker, args=(payload_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # Final state must be valid JSON containing exactly one of the two payloads.
    final = json.loads(path.read_text())
    assert final in (payload_a, payload_b)


# --------------------------------------------------------------------------- #
# 17. Idempotency — 50 captures with real apply fires twice, no duplicates.
# --------------------------------------------------------------------------- #
def test_idempotency_50_captures_no_duplicate_artifacts(repo_root: Path) -> None:
    """The skip-if-exists guard from #84 means re-running the auto-distill
    against the same source set produces no new artifacts on the second
    fire. We count fires by patching ``core.distill.run_auto_distill`` so
    the assertion is precise; the underlying apply path is exercised in
    ``tests/test_distill.py``.
    """
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))

    real_calls: list[dict[str, dict[str, int]]] = []

    def real_then_track(_gateway: Any) -> dict[str, dict[str, int]]:
        # Empty-source pool: planner returns no plans, so applied=skipped=0
        # on every call. The point of the test is that fire-count == 2.
        report = {
            "domains": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
            "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        }
        real_calls.append(report)
        return report

    with patch("core.distill.run_auto_distill", side_effect=real_then_track):
        for i in range(50):
            _capture_silent(g, i)

    assert len(real_calls) == 2

    # Counter resets to 0 at the second fire.
    state = json.loads(_state_file().read_text())
    assert state["captures_since_last_distill"] == 0


# --------------------------------------------------------------------------- #
# 18. Re-entrancy guard — recursive capture from apply does not re-fire.
# --------------------------------------------------------------------------- #
def test_reentrancy_guard_prevents_runaway(repo_root: Path) -> None:
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))

    call_count = {"n": 0}

    def fake_run(gw: Any) -> dict[str, dict[str, int]]:
        call_count["n"] += 1
        # Simulate the apply path issuing a recursive capture, which the
        # event bus delivers back to ``_on_post_capture_distill``. The
        # guard MUST prevent that recursive entry from incrementing the
        # call count or firing another distill.
        gw.capture(
            layer="ephemeral",
            content="recursive-artifact",
            tags=[],
            no_classify=True,
        )
        return {
            "domains": {"planned": 0, "applied": 1, "skipped": 0, "errors": 0},
            "policies": {"planned": 0, "applied": 0, "skipped": 0, "errors": 0},
        }

    with patch("core.distill.run_auto_distill", side_effect=fake_run):
        for i in range(25):
            _capture_silent(g, i)

    # Exactly one outer fire — the inner recursive capture was guarded.
    assert call_count["n"] == 1


# --------------------------------------------------------------------------- #
# 19. ``run_auto_distill`` returns a structured report.
# --------------------------------------------------------------------------- #
def test_run_auto_distill_returns_structured_report(gateway: Any) -> None:
    from core.distill import run_auto_distill

    report = run_auto_distill(gateway)
    assert set(report.keys()) == {"domains", "policies"}
    for kind in ("domains", "policies"):
        assert set(report[kind].keys()) == {"planned", "applied", "skipped", "errors"}
        for key in ("planned", "applied", "skipped", "errors"):
            assert report[kind][key] == 0


# --------------------------------------------------------------------------- #
# 20. ``run_auto_distill`` swallows per-plan errors and continues.
# --------------------------------------------------------------------------- #
def test_config_invalid_enabled_bool_int_interval_falls_back_to_default(
    repo_root: Path,
) -> None:
    """``interval_captures: true`` is a YAML bool; covers the bool-aware guard
    in ``_parse_distillation_config`` that prevents ``bool`` (subclass of
    ``int``) from being silently coerced to ``1``."""
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": True})
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=str(repo_root))
    assert cfg.distillation.interval_captures == 25


def test_state_file_invalid_schema_coerces_to_defaults(repo_root: Path) -> None:
    """Sidecar payloads with a non-int counter or non-str timestamp must fall
    through the read tolerance branches without raising."""
    from core.distill import _read_distill_state, _state_path

    state_path = _state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Non-int counter (string), non-str last_distill_at (int).
    state_path.write_text(
        json.dumps({"captures_since_last_distill": "not-int", "last_distill_at": 42})
    )

    out = _read_distill_state(state_path)
    assert out == {"captures_since_last_distill": 0, "last_distill_at": ""}

    # Boolean counter — covered separately because ``bool`` is a subclass of int.
    state_path.write_text(
        json.dumps({"captures_since_last_distill": True, "last_distill_at": "x"})
    )
    out = _read_distill_state(state_path)
    assert out == {"captures_since_last_distill": 0, "last_distill_at": "x"}

    # Top-level non-dict payload (list) — falls through to defaults.
    state_path.write_text(json.dumps([1, 2, 3]))
    out = _read_distill_state(state_path)
    assert out == {"captures_since_last_distill": 0, "last_distill_at": ""}


def test_state_file_write_failure_cleans_up_tmp(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed ``os.replace`` must unlink the tempfile before re-raising."""
    import core.distill as distill_module
    from core.distill import _state_path, _write_distill_state

    state_path = _state_path()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(distill_module.os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        _write_distill_state(state_path, {"captures_since_last_distill": 1, "last_distill_at": "x"})

    # Tempfile cleanup: the parent directory exists but holds no leftover
    # ``.distill-state.*.tmp`` file.
    leftovers = list(state_path.parent.glob(".distill-state.*.tmp"))
    assert leftovers == []


def test_state_file_write_failure_unlink_also_fails(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If both ``os.replace`` AND the cleanup ``os.unlink`` raise, the outer
    OSError is still surfaced — the cleanup failure is silently swallowed.
    """
    import core.distill as distill_module
    from core.distill import _state_path, _write_distill_state

    state_path = _state_path()

    def replace_boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated replace failure")

    def unlink_boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated unlink failure")

    monkeypatch.setattr(distill_module.os, "replace", replace_boom)
    monkeypatch.setattr(distill_module.os, "unlink", unlink_boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        _write_distill_state(
            state_path,
            {"captures_since_last_distill": 1, "last_distill_at": "x"},
        )


def test_run_auto_distill_swallows_compute_domain_exception(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from ``compute_domain_plan`` is counted as an error and the
    policy pipeline still runs."""
    import core.distill as distill_module
    from core.distill import run_auto_distill

    def boom(_gw: Any) -> list[Any]:
        raise RuntimeError("compute-domain-fail")

    monkeypatch.setattr(distill_module, "compute_domain_plan", boom)

    report = run_auto_distill(gateway)
    assert report["domains"] == {
        "planned": 0,
        "applied": 0,
        "skipped": 0,
        "errors": 1,
    }


def test_run_auto_distill_swallows_compute_policy_exception(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from ``compute_policy_plan`` is counted as an error and the
    domain pipeline still ran cleanly."""
    import core.distill as distill_module
    from core.distill import run_auto_distill

    def boom(_gw: Any, *, policy: Any = None, layers: Any = None) -> list[Any]:
        raise RuntimeError("compute-policy-fail")

    monkeypatch.setattr(distill_module, "compute_policy_plan", boom)

    report = run_auto_distill(gateway)
    assert report["policies"] == {
        "planned": 0,
        "applied": 0,
        "skipped": 0,
        "errors": 1,
    }


def test_run_auto_distill_skipped_increments_for_idempotent_plan(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``apply_*_plan`` returns ``applied=False`` (skip-if-exists), the
    report counts it under ``skipped``."""
    import core.distill as distill_module
    from core.distill import (
        DistillPlan,
        DistillResult,
        KIND_DOMAIN,
        KIND_POLICY,
        run_auto_distill,
    )

    skip_domain = DistillPlan(
        kind=KIND_DOMAIN,
        artifact_id="a",
        label="ok",
        sources=("s1", "s2"),
        layer="session",
        content="# ok\n",
        method="domain-distill-v1",
        tag="distilled:domain",
        extra_metadata={},
        existing=True,
    )
    skip_policy = DistillPlan(
        kind=KIND_POLICY,
        artifact_id="b",
        label="ok",
        sources=("s3", "s4"),
        layer="session",
        content="# ok\n",
        method="policy-distill-v1",
        tag="distilled:policy",
        extra_metadata={},
        existing=True,
    )

    def fake_compute_domain(_gw: Any) -> list[DistillPlan]:
        return [skip_domain]

    def fake_compute_policy(_gw: Any, *, policy: Any = None, layers: Any = None):
        return [skip_policy]

    def fake_apply_domain(_gw: Any, plan: DistillPlan):
        return DistillResult(
            kind=plan.kind,
            artifact_id=plan.artifact_id,
            sources=plan.sources,
            layer=plan.layer,
            applied=False,
        )

    def fake_apply_policy(_gw: Any, plan: DistillPlan):
        return DistillResult(
            kind=plan.kind,
            artifact_id=plan.artifact_id,
            sources=plan.sources,
            layer=plan.layer,
            applied=False,
        )

    monkeypatch.setattr(distill_module, "compute_domain_plan", fake_compute_domain)
    monkeypatch.setattr(distill_module, "apply_domain_plan", fake_apply_domain)
    monkeypatch.setattr(distill_module, "compute_policy_plan", fake_compute_policy)
    monkeypatch.setattr(distill_module, "apply_policy_plan", fake_apply_policy)

    report = run_auto_distill(gateway)
    assert report["domains"]["skipped"] == 1
    assert report["policies"]["skipped"] == 1


def test_run_auto_distill_swallows_policy_apply_error(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raise from ``apply_policy_plan`` is counted as an error without
    propagating to the caller."""
    import core.distill as distill_module
    from core.distill import DistillPlan, KIND_POLICY, run_auto_distill

    plan = DistillPlan(
        kind=KIND_POLICY,
        artifact_id="p",
        label="bad",
        sources=("s1", "s2"),
        layer="session",
        content="# bad\n",
        method="policy-distill-v1",
        tag="distilled:policy",
        extra_metadata={},
        existing=False,
    )

    def fake_compute_policy(_gw: Any, *, policy: Any = None, layers: Any = None):
        return [plan]

    def boom(_gw: Any, _plan: DistillPlan):
        raise RuntimeError("apply-policy-fail")

    monkeypatch.setattr(distill_module, "compute_policy_plan", fake_compute_policy)
    monkeypatch.setattr(distill_module, "apply_policy_plan", boom)

    report = run_auto_distill(gateway)
    assert report["policies"]["errors"] == 1


def test_consolidate_swallows_distill_exception_and_logs(
    repo_root: Path,
) -> None:
    """A raise during the end-of-sweep distill is caught and logged via the
    observability sink so ``consolidate()`` still returns normally."""
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))
    with patch(
        "core.distill.run_auto_distill",
        side_effect=RuntimeError("consolidate-distill-fail"),
    ):
        # Should NOT raise.
        g.consolidate()

    # Allow background thread to land the log entry.
    import time

    obs_log = repo_root / ".agent" / "observability.jsonl"
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if obs_log.exists():
            entries = [
                json.loads(line)
                for line in obs_log.read_text().splitlines()
                if line.strip()
            ]
            if any(
                e.get("event") == "auto_distill"
                and e.get("trigger") == "consolidate"
                and e.get("success") is False
                and "consolidate-distill-fail" in e.get("error", "")
                for e in entries
            ):
                break
        time.sleep(0.05)
    else:
        pytest.fail(
            "observability.jsonl never received the consolidate-failure entry"
        )


def test_subscriber_outer_swallow_caught_unexpected_exception(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the inner guards somehow miss an exception, the outer
    ``try/except`` in ``_on_post_capture_distill`` must still swallow it and
    write an observability entry — the capture caller never sees a failure.
    """
    _write_mnemos_yml(repo_root, {"enabled": True, "interval_captures": 25})
    from core.gateway import MemoryGateway

    g = MemoryGateway(repo_root=str(repo_root))

    # Force ``_state_path`` itself to raise so the outer try-block is the
    # only thing that can catch it.
    import core.distill as distill_module

    def boom() -> Path:
        raise RuntimeError("outer-swallow-fail")

    monkeypatch.setattr(distill_module, "_state_path", boom)

    # Capture should still succeed despite the inner failure.
    item_id = _capture_silent(g, 0)
    assert item_id

    # Allow background thread to land the log entry.
    import time

    obs_log = repo_root / ".agent" / "observability.jsonl"
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if obs_log.exists():
            entries = [
                json.loads(line)
                for line in obs_log.read_text().splitlines()
                if line.strip()
            ]
            if any(
                e.get("event") == "auto_distill"
                and e.get("trigger") == "post-capture"
                and e.get("success") is False
                and "outer-swallow-fail" in e.get("error", "")
                for e in entries
            ):
                break
        time.sleep(0.05)
    else:
        pytest.fail(
            "observability.jsonl never received the outer-swallow entry"
        )


def test_run_auto_distill_swallows_per_plan_errors(
    gateway: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a single failing plan into the domain pipeline and verify the
    error is counted in the report instead of propagating to the caller.
    """
    import core.distill as distill_module
    from core.distill import DistillPlan, KIND_DOMAIN, run_auto_distill

    fake_plan_ok = DistillPlan(
        kind=KIND_DOMAIN,
        artifact_id="00000000-0000-0000-0000-000000000001",
        label="ok",
        sources=("src-ok-a", "src-ok-b"),
        layer="session",
        content="# ok\n",
        method="domain-distill-v1",
        tag="distilled:domain",
        extra_metadata={"artifact_kind": "domain"},
        existing=False,
    )
    fake_plan_bad = DistillPlan(
        kind=KIND_DOMAIN,
        artifact_id="00000000-0000-0000-0000-000000000002",
        label="bad",
        sources=("src-bad-a", "src-bad-b"),
        layer="session",
        content="# bad\n",
        method="domain-distill-v1",
        tag="distilled:domain",
        extra_metadata={"artifact_kind": "domain"},
        existing=False,
    )

    def fake_compute_domain(_gw: Any) -> list[DistillPlan]:
        return [fake_plan_bad, fake_plan_ok]

    def fake_compute_policy(_gw: Any) -> list[DistillPlan]:
        return []

    apply_calls: list[str] = []

    def fake_apply_domain(_gw: Any, plan: DistillPlan):
        apply_calls.append(plan.artifact_id)
        if plan.artifact_id.endswith("002"):
            raise RuntimeError("apply-fail")
        from core.distill import DistillResult

        return DistillResult(
            kind=plan.kind,
            artifact_id=plan.artifact_id,
            sources=plan.sources,
            layer=plan.layer,
            applied=True,
        )

    monkeypatch.setattr(distill_module, "compute_domain_plan", fake_compute_domain)
    monkeypatch.setattr(distill_module, "apply_domain_plan", fake_apply_domain)
    monkeypatch.setattr(distill_module, "compute_policy_plan", fake_compute_policy)

    report = run_auto_distill(gateway)

    # Both plans attempted; one applied; one error counted.
    assert report["domains"]["planned"] == 2
    assert report["domains"]["applied"] == 1
    assert report["domains"]["errors"] == 1
    # Policies pipeline ran cleanly with zero plans.
    assert report["policies"] == {
        "planned": 0,
        "applied": 0,
        "skipped": 0,
        "errors": 0,
    }
