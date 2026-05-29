"""Sync round-trip test for distillation lineage metadata (Issue #84).

Verifies that the additive front-matter introduced by distillation
(``artifact_kind`` / ``sources`` / ``distillation_method`` on the artifact and
``distilled_into`` on each source) survives the full
``write → sync_commit → push → fresh-clone read`` cycle through the default
:class:`core.store.MemoryStore` backend.

Helpers reused (do NOT reimplement): bare-repo / sync-config / identity setup
follows the patterns established in ``tests/test_store_sync.py`` and
``tests/test_sync_e2e.py`` — mirrored here exactly as
``tests/test_compaction_sync_roundtrip.py`` does for #81.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from core.config import SyncConfig


# --------------------------------------------------------------------------- #
# Helpers — mirror tests/test_store_sync.py
# --------------------------------------------------------------------------- #

def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )


def _setup_identity(path: Path) -> None:
    _git("config", "user.email", "test@mnemos.test", cwd=path)
    _git("config", "user.name", "mnemos Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)


def _make_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", cwd=path)
    return path


def _seed_repo(repo: Path, bare: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=repo)
    _setup_identity(repo)
    subprocess.run(
        ["git", "checkout", "-b", "main"], cwd=str(repo),
        capture_output=True, text=True,
    )
    (repo / "README.md").write_text("root readme\n", encoding="utf-8")
    wiki = repo / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True, exist_ok=True)
    (wiki / ".gitkeep").write_text("", encoding="utf-8")
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
    agent = repo / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True, exist_ok=True)
    (agent / "workflows" / "hooks").mkdir(parents=True, exist_ok=True)

    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", str(bare), cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)


def _sync_config() -> SyncConfig:
    return SyncConfig(
        enabled=True,
        remote="origin",
        branch="main",
        mode="auto",
        auto_pull_on_capture=True,
        auto_push_after_commit=True,
        pull_rate_limit_seconds=0,
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate_pull_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    return _make_bare(tmp_path / "bare.git")


@pytest.fixture
def writer_repo(tmp_path: Path, bare_remote: Path) -> Path:
    repo = tmp_path / "writer"
    _seed_repo(repo, bare_remote)
    return repo


# --------------------------------------------------------------------------- #
# Round-trip test
# --------------------------------------------------------------------------- #

def test_distill_metadata_survives_git_sync_roundtrip(
    writer_repo: Path, bare_remote: Path, tmp_path: Path,
) -> None:
    """Capture + apply_domain_plan on writer → push to bare → clone elsewhere.

    Assertion: the cloned repo reads back the distilled artifact with the same
    ``sources`` / ``artifact_kind`` / ``distillation_method``, and each source
    still carries the additive ``distilled_into`` back-pointer pointing at the
    artifact id.
    """
    from core.distill import apply_domain_plan, compute_domain_plan
    from core.gateway import MemoryGateway

    import os
    os.environ["MNEMOS_REPO_ROOT"] = str(writer_repo)
    gw = MemoryGateway(repo_root=str(writer_repo))

    from core.store import MemoryStore
    gw._store = MemoryStore(
        repo_root=str(writer_repo),
        sync_config=_sync_config(),
    )
    from core.search import SearchMiddleware
    gw._search = SearchMiddleware(
        repo_root=str(writer_repo),
        fts_index=gw._fts,
        store=gw._store,
    )

    # Capture two backend-tagged items directly into wiki-mapped layers so the
    # sync engine commits + pushes them, then distill the derived domain.
    id1 = gw.capture(
        layer="project", content="backend roundtrip one",
        tags=["agent:backend"], no_classify=True,
    )
    id2 = gw.capture(
        layer="global", content="backend roundtrip two",
        tags=["agent:backend"], no_classify=True,
    )
    assert id1 and id2

    plan = next(p for p in compute_domain_plan(gw))
    result = apply_domain_plan(gw, plan)
    assert result.applied is True

    gw._store.sync_commit(message="distill: lineage metadata round-trip test")
    gw._store.sync_push()

    clone_dir = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(clone_dir)],
        capture_output=True, text=True, check=True,
    )
    _setup_identity(clone_dir)

    os.environ["MNEMOS_REPO_ROOT"] = str(clone_dir)
    gw_clone = MemoryGateway(repo_root=str(clone_dir))

    artifact = gw_clone._store.read(plan.artifact_id)
    assert artifact.get("artifact_kind") == "domain"
    assert artifact.get("distillation_method") == "domain-distill-v1"
    assert set(artifact.get("sources", [])) == {id1, id2}

    for source_id in (id1, id2):
        src = gw_clone._store.read(source_id)
        assert plan.artifact_id in src.get("distilled_into", [])
        # Non-destructive: the source is never archived by distillation.
        assert src.get("stage") != "archived"
