"""Lifecycle + lineage tests for final-memory distillation (Issue #84).

These tests verify the persistence/management contract that #67's read-only
derivation lacks:

* domain / policy planners are PURE — ``compute_*`` writes nothing.
* artifact ids are deterministic — same kind + same source-set ⇒ same id + body.
* ``apply_*`` is idempotent — a second run writes no duplicate artifact and does
  not double-append ``distilled_into``.
* lineage is non-destructive AND bidirectional — the artifact carries
  ``sources`` and each source gains an append-only ``distilled_into``; sources
  are never archived or superseded.
* the artifact layer is PolicyEngine-derived (never hard-coded).
* the feedback-loop guard excludes prior ``artifact_kind`` items from re-derivation.
* ``restore_distilled_source`` recovers the source content + its back-pointers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


# --------------------------------------------------------------------------- #
# Repo / gateway fixtures (mirror tests/test_compaction_lifecycle.py)
# --------------------------------------------------------------------------- #

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)
    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

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
    return tmp_path


@pytest.fixture
def gateway(repo_root: Path):
    from core.gateway import MemoryGateway
    return MemoryGateway(repo_root=str(repo_root))


def _seed_domain(gateway: Any) -> list[str]:
    """Two backend-tagged items spanning two layers (one derived domain)."""
    id1 = gateway.capture(
        layer="project", content="backend api one", tags=["agent:backend"], no_classify=True
    )
    id2 = gateway.capture(
        layer="global", content="backend api two", tags=["agent:backend"], no_classify=True
    )
    assert id1 and id2
    return [id1, id2]


def _seed_policy(gateway: Any) -> list[str]:
    """Two constraint-themed items spanning two layers (one policy cluster)."""
    id1 = gateway.capture(
        layer="session", content="never push to remote one",
        tags=["constraint:no-push"], no_classify=True,
    )
    id2 = gateway.capture(
        layer="project", content="never push to remote two",
        tags=["constraint:no-push"], no_classify=True,
    )
    assert id1 and id2
    return [id1, id2]


# --------------------------------------------------------------------------- #
# Planner purity (AC: review/compute writes nothing)
# --------------------------------------------------------------------------- #

class TestPlannerPurity:
    def test_compute_domain_plan_writes_nothing(self, gateway: Any, repo_root: Path) -> None:
        from core.distill import compute_domain_plan

        _seed_domain(gateway)
        before = set(repo_root.rglob("*.md"))
        plans = compute_domain_plan(gateway)
        after = set(repo_root.rglob("*.md"))

        assert before == after
        assert any(p.kind == "domain" for p in plans)

    def test_compute_policy_plan_writes_nothing(self, gateway: Any, repo_root: Path) -> None:
        from core.distill import compute_policy_plan

        _seed_policy(gateway)
        before = set(repo_root.rglob("*.md"))
        plans = compute_policy_plan(gateway)
        after = set(repo_root.rglob("*.md"))

        assert before == after
        assert any(p.kind == "policy" for p in plans)

    def test_compute_policy_plan_accepts_explicit_policy(self, gateway: Any) -> None:
        from core.distill import compute_policy_plan

        _seed_policy(gateway)
        plans = compute_policy_plan(gateway, policy=gateway._policy)
        assert any(p.kind == "policy" for p in plans)


# --------------------------------------------------------------------------- #
# Determinism (AC: same kind + source-set ⇒ identical id + body)
# --------------------------------------------------------------------------- #

class TestDeterminism:
    def test_same_source_set_yields_same_id_and_body(self, gateway: Any) -> None:
        from core.distill import compute_domain_plan

        _seed_domain(gateway)
        first = {p.artifact_id: p for p in compute_domain_plan(gateway)}
        second = {p.artifact_id: p for p in compute_domain_plan(gateway)}

        assert set(first) == set(second)
        for artifact_id, plan in first.items():
            assert plan.content == second[artifact_id].content
            assert plan.sources == second[artifact_id].sources

    def test_artifact_id_independent_of_source_order(self, gateway: Any) -> None:
        from core.distill import _artifact_id

        a = _artifact_id("domain", ["b", "a", "c"])
        b = _artifact_id("domain", ["c", "b", "a"])
        assert a == b

    def test_kind_changes_artifact_id(self, gateway: Any) -> None:
        from core.distill import _artifact_id

        assert _artifact_id("domain", ["a", "b"]) != _artifact_id("policy", ["a", "b"])


# --------------------------------------------------------------------------- #
# Idempotency (AC: re-run writes no duplicate + no double back-link)
# --------------------------------------------------------------------------- #

class TestIdempotency:
    def test_second_apply_is_a_noop(self, gateway: Any) -> None:
        from core.distill import apply_domain_plan, compute_domain_plan

        _seed_domain(gateway)
        plan = next(p for p in compute_domain_plan(gateway))

        first = apply_domain_plan(gateway, plan)
        assert first.applied is True

        # Re-compute (plan now flags existing) and re-apply.
        plan_again = next(
            p for p in compute_domain_plan(gateway) if p.artifact_id == plan.artifact_id
        )
        assert plan_again.existing is True
        second = apply_domain_plan(gateway, plan_again)
        assert second.applied is False

    def test_reapply_does_not_double_append_distilled_into(self, gateway: Any) -> None:
        from core.distill import (
            apply_domain_plan,
            compute_domain_plan,
            restore_distilled_source,
        )

        source_ids = _seed_domain(gateway)
        plan = next(p for p in compute_domain_plan(gateway))
        apply_domain_plan(gateway, plan)
        apply_domain_plan(gateway, plan)  # second apply must not re-link

        for sid in source_ids:
            snap = restore_distilled_source(gateway, sid)
            assert snap["distilled_into"].count(plan.artifact_id) == 1


# --------------------------------------------------------------------------- #
# Non-destructive + bidirectional lineage (AC: sources never archived)
# --------------------------------------------------------------------------- #

class TestNonDestructiveLineage:
    def test_sources_retain_stage_and_layer(self, gateway: Any) -> None:
        from core.distill import apply_domain_plan, compute_domain_plan

        source_ids = _seed_domain(gateway)
        before = {sid: gateway._store.read(sid) for sid in source_ids}

        plan = next(p for p in compute_domain_plan(gateway))
        apply_domain_plan(gateway, plan)

        for sid in source_ids:
            after = gateway._store.read(sid)
            assert after["stage"] == before[sid]["stage"]
            assert after["stage"] != "archived"
            assert after["layer"] == before[sid]["layer"]
            assert "superseded_by" not in after

    def test_bidirectional_lineage_reconstructs(self, gateway: Any) -> None:
        from core.distill import apply_domain_plan, compute_domain_plan

        source_ids = _seed_domain(gateway)
        plan = next(p for p in compute_domain_plan(gateway))
        apply_domain_plan(gateway, plan)

        artifact = gateway._store.read(plan.artifact_id)
        assert set(artifact["sources"]) == set(source_ids)
        assert artifact["artifact_kind"] == "domain"

        for sid in source_ids:
            src = gateway._store.read(sid)
            assert plan.artifact_id in src["distilled_into"]

    def test_multiple_contributions_append_not_overwrite(self, gateway: Any) -> None:
        from core.distill import (
            apply_domain_plan,
            apply_policy_plan,
            compute_domain_plan,
            compute_policy_plan,
            restore_distilled_source,
        )

        # An item tagged with both an agent: and a constraint: namespace
        # contributes to a domain AND a policy cluster — so its distilled_into
        # accumulates multiple ids by APPEND, not overwrite.
        shared = gateway.capture(
            layer="project", content="shared backend constraint memo",
            tags=["agent:backend", "constraint:shared"], no_classify=True,
        )
        gateway.capture(
            layer="global", content="another backend memo",
            tags=["agent:backend"], no_classify=True,
        )
        gateway.capture(
            layer="session", content="another constraint memo",
            tags=["constraint:shared"], no_classify=True,
        )
        assert shared

        applied_ids: set[str] = set()
        for plan in compute_domain_plan(gateway):
            if shared in plan.sources:
                applied_ids.add(plan.artifact_id)
            apply_domain_plan(gateway, plan)
        for plan in compute_policy_plan(gateway):
            if shared in plan.sources:
                applied_ids.add(plan.artifact_id)
            apply_policy_plan(gateway, plan)

        snap = restore_distilled_source(gateway, shared)
        # The back-pointer accumulated every artifact this source fed (append),
        # spans both kinds, and contains no duplicates (dedupe on append).
        kinds = {gateway._store.read(aid)["artifact_kind"] for aid in snap["distilled_into"]}
        assert kinds == {"domain", "policy"}
        assert set(snap["distilled_into"]) == applied_ids
        assert len(snap["distilled_into"]) == len(set(snap["distilled_into"]))
        assert len(applied_ids) >= 2


# --------------------------------------------------------------------------- #
# PolicyEngine-derived layer (AC: never hard-coded)
# --------------------------------------------------------------------------- #

class TestLayerDerivation:
    def test_mixed_layer_sources_land_on_topmost(self, gateway: Any) -> None:
        from core.distill import compute_domain_plan

        _seed_domain(gateway)  # project + global → global is terminal
        plan = next(p for p in compute_domain_plan(gateway))
        assert plan.layer == "global"

    def test_policy_layer_derived_from_sources(self, gateway: Any) -> None:
        from core.distill import compute_policy_plan

        _seed_policy(gateway)  # session + project → project
        plan = next(p for p in compute_policy_plan(gateway))
        assert plan.layer == "project"


# --------------------------------------------------------------------------- #
# Feedback-loop guard (AC: prior artifacts excluded from re-derivation)
# --------------------------------------------------------------------------- #

class TestFeedbackLoopGuard:
    def test_artifacts_excluded_from_source_pool(self, gateway: Any) -> None:
        from core.distill import (
            apply_domain_plan,
            collect_source_items,
            compute_domain_plan,
        )

        source_ids = _seed_domain(gateway)
        artifact_ids = []
        for plan in compute_domain_plan(gateway):
            apply_domain_plan(gateway, plan)
            artifact_ids.append(plan.artifact_id)

        pool_ids = {str(item.get("id")) for item in collect_source_items(gateway)}
        assert set(source_ids) <= pool_ids
        for artifact_id in artifact_ids:
            assert artifact_id not in pool_ids

    def test_rerun_after_apply_creates_no_new_artifacts(self, gateway: Any) -> None:
        from core.distill import apply_domain_plan, compute_domain_plan

        _seed_domain(gateway)
        for plan in compute_domain_plan(gateway):
            apply_domain_plan(gateway, plan)

        # Re-running over the now-distilled store yields only existing plans.
        rerun = compute_domain_plan(gateway)
        assert all(p.existing for p in rerun)


# --------------------------------------------------------------------------- #
# Audit navigation (AC: restore_distilled_source returns source content)
# --------------------------------------------------------------------------- #

class TestRestoreDistilledSource:
    def test_returns_content_and_empty_back_pointer_when_undistilled(
        self, gateway: Any
    ) -> None:
        from core.distill import restore_distilled_source

        source_ids = _seed_domain(gateway)
        snap = restore_distilled_source(gateway, source_ids[0])
        assert "backend api" in snap["content"]
        assert snap["distilled_into"] == []

    def test_surfaces_back_pointer_after_apply(self, gateway: Any) -> None:
        from core.distill import (
            apply_domain_plan,
            compute_domain_plan,
            restore_distilled_source,
        )

        source_ids = _seed_domain(gateway)
        plan = next(p for p in compute_domain_plan(gateway))
        apply_domain_plan(gateway, plan)

        snap = restore_distilled_source(gateway, source_ids[0])
        assert snap["distilled_into"] == [plan.artifact_id]


# --------------------------------------------------------------------------- #
# Edge cases (coverage of skip / missing-source paths)
# --------------------------------------------------------------------------- #

class TestEdgeCases:
    def test_empty_store_yields_no_plans(self, gateway: Any) -> None:
        from core.distill import compute_domain_plan, compute_policy_plan

        assert compute_domain_plan(gateway) == []
        assert compute_policy_plan(gateway) == []

    def test_back_link_skips_missing_source(self, gateway: Any) -> None:
        from core.distill import _append_distilled_into

        # No such source → silent no-op (lineage stays in the artifact's sources).
        _append_distilled_into(gateway, "no-such-source-id", "artifact-id")

    def test_back_link_dedupes_repeat_artifact(self, gateway: Any) -> None:
        from core.distill import _append_distilled_into, restore_distilled_source

        source_ids = _seed_domain(gateway)
        sid = source_ids[0]
        _append_distilled_into(gateway, sid, "artifact-xyz")
        # A second append of the same id is an early-return no-op (dedupe).
        _append_distilled_into(gateway, sid, "artifact-xyz")

        snap = restore_distilled_source(gateway, sid)
        assert snap["distilled_into"] == ["artifact-xyz"]

    def test_collect_source_items_layer_filter(self, gateway: Any) -> None:
        from core.distill import collect_source_items

        _seed_domain(gateway)  # project + global
        only_global = collect_source_items(gateway, layers=["global"])
        assert all(item.get("layer") == "global" for item in only_global)
