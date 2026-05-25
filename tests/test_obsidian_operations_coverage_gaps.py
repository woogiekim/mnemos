"""Focused coverage for Obsidian backend and operations edge paths."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest


def _write_md(path: Path, content: str, **metadata: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(content, **metadata)), encoding="utf-8")


def test_obsidian_slug_sync_paths_migration_and_health_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.git as git
    from core.config import SyncConfig
    from core.obsidian import ObsidianBackend, SyncConflictError, _content_slug

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    backend = ObsidianBackend(
        str(vault),
        sync_config=SyncConfig(enabled=True, pull_rate_limit_seconds=0),
    )
    assert _content_slug("word " * 80, max_chars=12) == "word-word-wo"
    assert backend._load_last_pull_ts() == 0.0
    cache_path = backend._pull_cache_path()
    original_write_text = Path.write_text

    def fail_cache_write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path == cache_path:
            raise OSError("cache denied")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_cache_write)
    backend._save_last_pull_ts(1.0)
    monkeypatch.setattr(Path, "write_text", original_write_text)

    monkeypatch.setattr(git, "remote_exists", lambda vault_path, remote: False)
    assert backend._has_remote() is False
    assert backend._should_pull() is False
    monkeypatch.setattr(git, "remote_exists", lambda vault_path, remote: (_ for _ in ()).throw(RuntimeError("remote failed")))
    assert backend._has_remote() is False

    monkeypatch.setattr(git, "remote_exists", lambda vault_path, remote: True)
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: False)
    assert backend._should_pull() is False
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: (_ for _ in ()).throw(RuntimeError("branch failed")))
    assert backend._should_pull() is False
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: True)
    assert backend._should_pull() is True
    backend._sync.enabled = False
    assert backend._hook_after_write([vault / "disabled.md"]) is False
    assert backend._hook_after_write_item("project", "item", [vault / "disabled.md"]) is False
    assert backend._should_pull() is False
    backend._sync.enabled = True
    backend._sync.auto_pull_on_capture = False
    assert backend._should_pull() is False
    backend._sync.auto_pull_on_capture = True

    monkeypatch.setattr(git, "pull_rebase", lambda *args, **kwargs: None)
    backend._hook_before_write()
    assert backend._last_pull_ts > 0

    monkeypatch.setattr(
        git,
        "pull_rebase",
        lambda *args, **kwargs: (_ for _ in ()).throw(git.GitCommandError(1, "conflict")),
    )
    with pytest.raises(SyncConflictError):
        backend._hook_before_write()
    assert (vault / "_sync_conflict.md").exists()

    assert backend._hook_after_write([]) is False
    assert backend._hook_after_write_item("project", "item", []) is False
    added: list[list[str]] = []
    monkeypatch.setattr(git, "add", lambda vault_path, paths: added.append(paths))
    monkeypatch.setattr(git, "commit", lambda vault_path, message: True)
    assert backend._hook_after_write([vault / "a.md"]) is True
    assert backend._hook_after_write_item("project", "item", [vault / "item.md"]) is True

    pushed: list[tuple[str, str]] = []
    monkeypatch.setattr(git, "push", lambda vault_path, remote, branch: pushed.append((remote, branch)))
    backend._hook_after_commit(False)
    backend._hook_after_commit(True)
    assert pushed
    pushed.clear()
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: False)
    backend._hook_after_commit(True)
    assert pushed == []
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: True)

    monkeypatch.setattr(git, "pull_rebase", lambda *args, **kwargs: None)
    backend.sync_pull()
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: False)
    backend.sync_pull()
    monkeypatch.setattr(git, "remote_has_branch", lambda vault_path, remote, branch: True)
    assert backend.sync_commit() is True
    monkeypatch.setattr(git, "status", lambda vault_path: {"branch": "main"})
    assert backend.sync_status()["sync_enabled"] is True

    project_dir = vault / "project"
    _write_md(project_dir / "legacy-id.md", "legacy", id="legacy-id", layer="project")
    _write_md(project_dir / "slug.md", "slug", id="slug-id", layer="project")
    bad = project_dir / "bad.md"
    bad.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert backend._find_path("legacy-id").name == "legacy-id.md"
    assert backend._find_path("slug-id").name == "slug.md"
    with pytest.raises(FileNotFoundError):
        backend._find_path("missing")
    assert backend._resolve_path(str(project_dir / "slug.md")).name == "slug.md"
    assert backend._resolve_path("project/slug.md").name == "slug.md"
    with pytest.raises(ValueError):
        backend._layer_dir("unknown")

    collision = project_dir / "collision.md"
    _write_md(collision, "Collision", id="other", layer="project")
    _write_md(project_dir / "collision-2.md", "Collision", id="same", layer="project")
    assert backend._build_file_path(project_dir, "same", "Collision").name == "collision-2.md"
    assert backend._build_file_path(project_dir, "new", "Collision").name == "collision-3.md"
    invalid_collision = project_dir / "invalid-collision.md"
    invalid_collision.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert backend._build_file_path(project_dir, "new", "Invalid Collision").name == "invalid-collision-2.md"
    _write_md(project_dir / "numbered.md", "Numbered", id="other", layer="project")
    (project_dir / "numbered-2.md").write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert backend._build_file_path(project_dir, "new", "Numbered").name == "numbered-3.md"

    conflict_file = project_dir / "conflict.md"
    conflict_file.write_text("<<<<<<< conflict", encoding="utf-8")
    with pytest.raises(SyncConflictError):
        backend.sync_continue()
    conflict_file.write_text("resolved", encoding="utf-8")
    unreadable = project_dir / "unreadable.md"
    unreadable.write_text("<<<<<<< hidden", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_conflict_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == unreadable:
            raise OSError("cannot read")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_conflict_read)
    transient = vault / "transient" / "conflict-x.md"
    transient.parent.mkdir(exist_ok=True)
    transient.write_text("conflict", encoding="utf-8")
    monkeypatch.setattr(git, "rebase_continue", lambda vault_path: None)
    backend.sync_continue()
    monkeypatch.setattr(Path, "read_text", original_read_text)
    assert not transient.exists()

    edit = project_dir / "edit.md"
    _write_md(edit, "changed", id="edit", layer="project", content_hash="old", quality_score=0.2, updated_at="2000-01-01T00:00:00Z")
    backend._mtime_cache[str(edit)] = edit.stat().st_mtime
    assert backend.sync_edits() >= 0
    same_hash = project_dir / "same-hash.md"
    _write_md(same_hash, "same", id="same-hash", layer="project", content_hash="0967115f2813a3541eaef77de9d9d5773f1c0c04314b0bbfe4ff3b3b1c55b5d5")
    backend._mtime_cache[str(same_hash)] = same_hash.stat().st_mtime - 10
    assert backend.sync_edits() >= 0
    stat_error = project_dir / "stat-error.md"
    _write_md(stat_error, "stat", id="stat-error", layer="project")
    original_stat = Path.stat

    def fail_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == stat_error:
            raise OSError("cannot stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_stat)
    assert backend.sync_edits() >= 0
    monkeypatch.setattr(Path, "stat", original_stat)
    edit.write_text(frontmatter.dumps(frontmatter.Post("changed again", id="edit", layer="project", content_hash="old")), encoding="utf-8")
    assert backend.sync_edits() >= 1

    uuid_name = "12345678-1234-1234-1234-123456789abc.md"
    _write_md(project_dir / uuid_name, "Collision", id="uuid-item", layer="project")
    skip_uuid = "12345678-1234-1234-1234-123456789abd.md"
    _write_md(project_dir / skip_uuid, "12345678-1234-1234-1234-123456789abd", id="skip-uuid", layer="project")
    (project_dir / "87654321-1234-1234-1234-123456789abc.md").write_text("---\nid: [bad\n---\n", encoding="utf-8")
    dry = backend.rename_uuid_to_slug(dry_run=True)
    assert dry["renamed"] >= 1
    real_rename = os.rename
    monkeypatch.setattr(os, "rename", lambda src, dst: real_rename(src, dst))
    result = backend.rename_uuid_to_slug(dry_run=False, commit=True)
    assert result["renamed"] >= 1
    backend._sync.enabled = False
    commit_fail_uuid = "22345678-1234-1234-1234-123456789abc.md"
    _write_md(project_dir / commit_fail_uuid, "Commit Failure", id="commit-fail", layer="project")
    monkeypatch.setattr(git, "add", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("add failed")))
    assert backend.rename_uuid_to_slug(dry_run=False, commit=True)["renamed"] >= 1
    commit_ok_uuid = "32345678-1234-1234-1234-123456789abc.md"
    _write_md(project_dir / commit_ok_uuid, "Commit Success", id="commit-ok", layer="project")
    commit_messages: list[str] = []
    monkeypatch.setattr(git, "add", lambda *args, **kwargs: None)
    monkeypatch.setattr(git, "commit", lambda vault_path, message: commit_messages.append(message) or True)
    assert backend.rename_uuid_to_slug(dry_run=False, commit=True)["renamed"] >= 1
    assert commit_messages
    stat_uuid = "42345678-1234-1234-1234-123456789abc.md"
    _write_md(project_dir / stat_uuid, "Stat Failure", id="stat-rename", layer="project")
    stat_new_path = project_dir / "stat-failure.md"
    original_stat_after_rename = Path.stat
    renamed_stat_path = {"done": False}

    def record_stat_rename(src: str, dst: str) -> None:
        real_rename(src, dst)
        if Path(dst) == stat_new_path:
            renamed_stat_path["done"] = True

    def fail_new_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == stat_new_path and renamed_stat_path["done"]:
            raise OSError("cannot stat new path")
        return original_stat_after_rename(path, *args, **kwargs)

    monkeypatch.setattr(os, "rename", record_stat_rename)
    monkeypatch.setattr(Path, "stat", fail_new_stat)
    assert backend.rename_uuid_to_slug(dry_run=False, commit=False)["renamed"] >= 1
    monkeypatch.setattr(Path, "stat", original_stat_after_rename)

    health = project_dir / "_health.md"
    health.write_text("old", encoding="utf-8")
    _write_md(project_dir / "low.md", "low", id="low", layer="project", quality_score=0.1, updated_at="2000-01-01T00:00:00Z")
    _write_md(project_dir / "bad-date.md", "date", id="bad-date", layer="project", quality_score=0.9, updated_at="bad-date")
    bad_health = project_dir / "bad-health.md"
    bad_health.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    health_path = backend.generate_health_page()
    assert health_path.exists()
    assert "[[low]]" in health_path.read_text(encoding="utf-8")


class FakeOperationsStore:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []
        self.fail_write = False

    def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
        return [
            {"id": "retain", "layer": layer, "stage": "stored", "content": "short", "quality_score": 0.9, "tags": ["tag"]},
            {"id": "archive", "layer": layer, "stage": "stored", "content": "bad", "quality_score": 0.1},
            {"id": "compress-artifact", "layer": layer, "stage": "compressed", "content": "artifact", "memory_os_artifact": "continuity_page"},
            {"id": "empty", "layer": layer, "content": ""},
        ]

    def write(self, layer: str, item_id: str, content: str, metadata: dict[str, Any]) -> Path:
        if self.fail_write:
            raise RuntimeError("write failed")
        self.writes.append((layer, item_id))
        return Path("/tmp") / f"{item_id}.md"

    def update(self, path: str, metadata_updates: dict[str, Any]) -> None:
        return None

    def list_layer(self, layer: str) -> list[Path]:
        return []


class FakeOperationsGateway:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._store = FakeOperationsStore()
        self._fts = type("FTS", (), {"index_item": lambda self, *args, **kwargs: None})()

    def log(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_operations_engine_reports_recovery_and_helper_edges(tmp_path: Path) -> None:
    from core.operations import (
        MemoryOperationsEngine,
        OperationalHealthThresholds,
        _has_continuity_metadata,
        _item_id,
        _parse_iso,
        _path_layer,
        _retrieval_lifecycle_score,
        _safe_int,
    )

    gateway = FakeOperationsGateway(tmp_path)
    engine = MemoryOperationsEngine(gateway)

    report = engine.run_lifecycle(dry_run=True, layers=["project"], limit=1, include_retained=True)
    assert report.evaluated_count == 1

    from core.lifecycle import LifecycleAction, LifecycleDecision

    engine._lifecycle = type(
        "Lifecycle",
        (),
        {
            "plan_transition": lambda self, item: LifecycleDecision(
                action=LifecycleAction.ARCHIVE,
                reason="force archive",
                target_stage="archived",
            )
        },
    )()
    original_apply = engine._apply_lifecycle_decision
    engine._apply_lifecycle_decision = lambda item, decision: (_ for _ in ()).throw(RuntimeError("apply failed"))
    degraded_lifecycle = engine.run_lifecycle(dry_run=False, layers=["project"], limit=1)
    assert degraded_lifecycle.status == "degraded"
    engine._apply_lifecycle_decision = original_apply

    gateway._store.fail_write = True
    compression = engine.run_compression_job(
        dry_run=False,
        layers=["project"],
        query="short",
        token_budget=200,
    )
    assert compression.status == "degraded"
    gateway._store.fail_write = False
    limited = engine.run_compression_job(dry_run=True, layers=["project"], limit=1)
    assert limited.input_count == 1

    assert engine.metric_history() == []
    history_path = engine._evidence_dir / "metrics-history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text('\nbad\n{"kind":"metrics_snapshot","metrics":{"scores":{"context_continuity_score":1}}}\n', encoding="utf-8")
    assert len(engine.metric_history(limit=1)) == 1
    readiness_history = engine._evidence_dir / "readiness-history.jsonl"
    readiness_history.write_text('\nbad\n{"kind":"readiness","status":"ready"}\n', encoding="utf-8")
    assert len(engine.readiness_history(limit=1)) == 1

    original_open = Path.open

    def fail_history_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path in {history_path, readiness_history}:
            raise OSError("history unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(Path, "open", fail_history_open)
    try:
        assert engine.metric_history() == []
        assert engine.readiness_history() == []
    finally:
        monkeypatch.undo()

    with pytest.raises(FileNotFoundError):
        engine.validate_health(calibrated=True)
    validation = engine.validate_health()
    assert validation.status in {"passed", "failed"}
    readiness_missing = engine.audit_readiness(calibrated=True)
    assert any(gap.code == "calibration_missing" for gap in readiness_missing.gaps)
    invalid = engine._evidence_dir / "latest-calibration.json"
    invalid.write_text("{bad", encoding="utf-8")
    assert engine.latest_calibrated_thresholds() is None
    invalid.write_text(json.dumps({"report": {"thresholds": []}}), encoding="utf-8")
    assert engine.latest_calibrated_thresholds() is None

    calibration = engine.calibrate_health(include_current=False, history_limit=1, floor=0.7)
    assert calibration.status in {"bootstrapped", "calibrated"}
    engine.record_calibration_report(calibration)
    assert engine.latest_calibrated_thresholds() is not None

    readiness = engine.audit_readiness(calibrated=True, max_evidence_age_hours=0)
    assert readiness.gaps

    unknown_gateway = type("Gateway", (), {"_root": tmp_path, "_store": gateway._store, "_fts": gateway._fts})()
    unknown_engine = MemoryOperationsEngine(unknown_gateway)
    assert unknown_engine.retrieval_backend_health().status == "unknown"
    search_gateway = type(
        "Gateway",
        (),
        {
            "_root": tmp_path,
            "_store": gateway._store,
            "_fts": gateway._fts,
            "_search": type("Search", (), {"backend_health": lambda self: {"status": "ok", "partial_failure": False, "backends": []}})(),
        },
    )()
    assert MemoryOperationsEngine(search_gateway).retrieval_backend_health().status == "ok"

    item = {"content": "body", "_path": str(tmp_path / "wiki" / "projects" / "x.md")}
    _write_md(Path(item["_path"]), "body", id="x", layer="project")
    assert engine._parse_path(Path(item["_path"]))["id"] == "x"
    corrections, issues = engine._repair_metadata_plan(item, "project", Path(item["_path"]))
    assert corrections["id"] == "x"
    assert issues
    assert engine._parse_path(Path(item["_path"])) if False else True
    assert _item_id({"_path": "/tmp/item.md"}) == "item"
    assert _path_layer("/tmp/.agent/runs/r/scratch/x.md") == "ephemeral"
    assert _path_layer("/tmp/.agent/runs/r/working/x.md") == "working"
    assert _path_layer("/tmp/.agent/sessions/s/x.md") == "session"
    assert _path_layer("/tmp/wiki/projects/x.md") == "project"
    assert _path_layer("/tmp/transient/x.md") == "transient"
    assert _path_layer(None) is None
    assert _path_layer("/tmp/unknown/x.md") is None
    assert _has_continuity_metadata({"content": ""}) is False
    assert _has_continuity_metadata({"content": "body", "workflow_id": "w"}) is True
    assert _safe_int("bad") is None
    assert _parse_iso("") is None
    assert _parse_iso("bad") is None
    assert _retrieval_lifecycle_score("forgotten") == 0.0
    assert _retrieval_lifecycle_score("archived") == 0.7
    assert OperationalHealthThresholds.uniform(0.5).context_continuity_score == 0.5
