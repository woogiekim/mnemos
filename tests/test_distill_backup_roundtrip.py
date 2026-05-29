"""Backup round-trip test for distillation lineage (Issue #84).

Confirms that the additive front-matter introduced by distillation survives a
full ``make_backup → restore_backup`` cycle into a fresh repo root without
bumping ``core/backup.py:SCHEMA_VERSION`` (the fields are additive YAML — no
schema change is needed). Mirrors ``tests/test_compaction_backup_roundtrip.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.backup import SCHEMA_VERSION, make_backup, restore_backup


# --------------------------------------------------------------------------- #
# Repo fixtures
# --------------------------------------------------------------------------- #

def _seed_minimal_repo(root: Path) -> None:
    wiki = root / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)
    agent = root / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy_cfg = {
        "layers": {
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
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    _seed_minimal_repo(root)
    return root


@pytest.fixture
def restore_root(tmp_path: Path) -> Path:
    root = tmp_path / "restore"
    _seed_minimal_repo(root)
    return root


# --------------------------------------------------------------------------- #
# Round-trip test
# --------------------------------------------------------------------------- #

def test_distill_metadata_survives_backup_restore(
    source_root: Path, restore_root: Path, tmp_path: Path,
) -> None:
    from core.distill import apply_domain_plan, compute_domain_plan
    from core.gateway import MemoryGateway

    # Capture + distill in source repo (project/global layers so backup picks
    # them up — backup walks wiki/<static-layer>/*.md only).
    gw_src = MemoryGateway(repo_root=str(source_root))
    id1 = gw_src.capture(
        layer="project", content="backend backup one",
        tags=["agent:backend"], no_classify=True,
    )
    id2 = gw_src.capture(
        layer="global", content="backend backup two",
        tags=["agent:backend"], no_classify=True,
    )
    assert id1 and id2

    plan = next(p for p in compute_domain_plan(gw_src))
    result = apply_domain_plan(gw_src, plan)
    assert result.applied is True

    archive_path = tmp_path / "distill-backup.tar.gz"
    written = make_backup(source_root, archive_path)
    assert Path(written).exists()

    report = restore_backup(archive_path, restore_root, overwrite=False)
    assert report.restored_count > 0

    # Backup schema must still be 1 — fields are additive YAML, no manifest change.
    assert SCHEMA_VERSION == 1

    gw_restored = MemoryGateway(repo_root=str(restore_root))
    artifact = gw_restored._store.read(plan.artifact_id)
    assert artifact.get("artifact_kind") == "domain"
    assert set(artifact.get("sources", [])) == {id1, id2}

    for source_id in (id1, id2):
        src = gw_restored._store.read(source_id)
        assert plan.artifact_id in src.get("distilled_into", [])
        assert src.get("stage") != "archived"
