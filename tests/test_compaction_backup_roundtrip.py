"""Backup round-trip test for compaction lineage (Issue #81 — Stage 2).

Confirms that the additive front-matter introduced by compaction
survives a full ``make_backup → restore_backup`` cycle into a fresh
repo root without bumping ``core/backup.py:SCHEMA_VERSION`` (the
fields are additive YAML — no schema change is needed).
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

def test_compaction_metadata_survives_backup_restore(
    source_root: Path, restore_root: Path, tmp_path: Path,
) -> None:
    from core.compaction import (
        COMPACTION_METHOD_DETERMINISTIC,
        apply_merge_plan,
        compute_merge_plan,
    )
    from core.gateway import MemoryGateway

    # Stage: capture + merge in source repo (project layer so backup
    # picks it up — backup walks wiki/<static-layer>/*.md only).
    gw_src = MemoryGateway(repo_root=str(source_root))
    id1 = gw_src.capture(layer="project", content="alpha beta backup one")
    id2 = gw_src.capture(layer="project", content="alpha beta backup two")
    assert id1 and id2

    group = [gw_src.read(id1), gw_src.read(id2)]
    result = apply_merge_plan(gw_src, compute_merge_plan(gw_src, group))
    assert result.merged_id

    # Stage: write archive.
    archive_path = tmp_path / "compaction-backup.tar.gz"
    written = make_backup(source_root, archive_path)
    assert Path(written).exists()

    # Stage: restore into a FRESH root.
    report = restore_backup(archive_path, restore_root, overwrite=False)
    assert report.restored_count > 0

    # Backup schema must still be 1 — we did NOT add any new manifest
    # field, and the per-item front-matter fields are additive YAML.
    assert SCHEMA_VERSION == 1

    # Stage: read the merged memory + sources back via a fresh gateway
    # on the restored root.  All three additive fields must persist.
    gw_restored = MemoryGateway(repo_root=str(restore_root))
    merged = gw_restored.read(result.merged_id)
    assert merged.get("compaction_method") == COMPACTION_METHOD_DETERMINISTIC
    assert set(merged.get("sources", [])) == {id1, id2}

    for source_id in (id1, id2):
        src = gw_restored.read(source_id)
        assert src.get("stage") == "archived"
        assert src.get("superseded_by") == result.merged_id
