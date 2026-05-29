"""Lifecycle + audit-trail tests for compaction (Issue #81 — Stage 2).

These tests verify that:

* ``derive_merged_layer`` is PolicyEngine-driven (no hard-coded layer
  names).  Different mixes of source layers yield the correct merged
  layer per the chain configured in policy.yaml.
* The audit trail is reconstructable after archive — every original
  source is recoverable via ``restore_source`` walking the merged
  memory's ``sources`` array.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


# --------------------------------------------------------------------------- #
# Repo / gateway fixtures
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


# --------------------------------------------------------------------------- #
# Layer derivation (acceptance criterion 5 — lifecycle-aware)
# --------------------------------------------------------------------------- #

class TestMergedLayerFollowsPolicy:
    def test_session_and_project_picks_project(self, gateway: Any) -> None:
        from core.compaction import derive_merged_layer
        sources = [
            {"id": "a", "layer": "session"},
            {"id": "b", "layer": "project"},
        ]
        # session promotes to project — project is the "higher" layer.
        assert derive_merged_layer(sources, gateway._policy) == "project"

    def test_working_and_session_picks_session(self, gateway: Any) -> None:
        from core.compaction import derive_merged_layer
        sources = [
            {"id": "a", "layer": "working"},
            {"id": "b", "layer": "session"},
        ]
        assert derive_merged_layer(sources, gateway._policy) == "session"

    def test_three_layer_chain_picks_topmost(self, gateway: Any) -> None:
        from core.compaction import derive_merged_layer
        sources = [
            {"id": "a", "layer": "working"},
            {"id": "b", "layer": "session"},
            {"id": "c", "layer": "project"},
        ]
        assert derive_merged_layer(sources, gateway._policy) == "project"

    def test_global_alone_picks_global(self, gateway: Any) -> None:
        from core.compaction import derive_merged_layer
        sources = [
            {"id": "a", "layer": "session"},
            {"id": "b", "layer": "global"},
        ]
        # session → project → global, so global is terminal.
        assert derive_merged_layer(sources, gateway._policy) == "global"


# --------------------------------------------------------------------------- #
# Audit trail reconstruction (acceptance criterion 7 — lineage)
# --------------------------------------------------------------------------- #

class TestAuditTrailReconstruction:
    def test_every_source_recoverable_via_restore_source(
        self, gateway: Any
    ) -> None:
        from core.compaction import (
            apply_merge_plan,
            compute_merge_plan,
            restore_source,
        )

        # Capture three distinct-but-similar items in the session layer.
        contents = [
            "alpha beta gamma delta — source one",
            "alpha beta gamma delta — source two",
            "alpha beta gamma delta — source three",
        ]
        ids: list[str] = []
        for text in contents:
            captured = gateway.capture(layer="session", content=text)
            assert captured
            ids.append(captured)

        group = [gateway.read(_id) for _id in ids]
        plan = compute_merge_plan(gateway, group)
        result = apply_merge_plan(gateway, plan)

        merged = gateway.read(result.merged_id)
        # The merged memory must list every source id.
        assert set(merged.get("sources", [])) == set(ids)

        # Walking each source via restore_source must recover the
        # original content and the supersede back-pointer.
        for original_text, source_id in zip(contents, ids):
            snap = restore_source(gateway, source_id)
            assert snap["id"] == source_id
            assert snap["stage"] == "archived"
            assert snap["superseded_by"] == result.merged_id
            # Original payload survived the archive.
            assert original_text.split(" — ")[1] in snap["content"]

    def test_merge_metadata_carries_compaction_method(self, gateway: Any) -> None:
        from core.compaction import (
            COMPACTION_METHOD_DETERMINISTIC,
            apply_merge_plan,
            compute_merge_plan,
        )

        id1 = gateway.capture(layer="session", content="alpha beta gamma one")
        id2 = gateway.capture(layer="session", content="alpha beta gamma two")
        assert id1 and id2
        group = [gateway.read(id1), gateway.read(id2)]
        result = apply_merge_plan(gateway, compute_merge_plan(gateway, group))

        merged = gateway.read(result.merged_id)
        assert merged.get("compaction_method") == COMPACTION_METHOD_DETERMINISTIC

    def test_sources_list_preserved_across_processes(
        self, gateway: Any, repo_root: Path
    ) -> None:
        """Re-instantiate a fresh gateway and confirm sources/superseded
        survive.  Mirrors what backup/sync/restore round-trips will see.
        """
        from core.compaction import apply_merge_plan, compute_merge_plan

        id1 = gateway.capture(layer="session", content="alpha beta one")
        id2 = gateway.capture(layer="session", content="alpha beta two")
        assert id1 and id2
        group = [gateway.read(id1), gateway.read(id2)]
        result = apply_merge_plan(gateway, compute_merge_plan(gateway, group))

        # Fresh gateway with no cached state.
        from core.gateway import MemoryGateway

        gw2 = MemoryGateway(repo_root=str(repo_root))
        merged_again = gw2.read(result.merged_id)
        assert set(merged_again.get("sources", [])) == {id1, id2}
        src_again = gw2.read(id1)
        assert src_again.get("stage") == "archived"
        assert src_again.get("superseded_by") == result.merged_id
