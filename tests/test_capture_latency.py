"""Regression tests for latency-sensitive capture paths."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from click.testing import CliRunner


def test_capture_dedup_uses_hash_index_without_full_store_scan(tmp_path: Path) -> None:
    from core.gateway import MemoryGateway, _capture_content_hash

    class Store:
        def find_by_content_hash(self, content_hash: str) -> tuple[str, str] | None:
            return ("existing-id", "project")

        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            raise AssertionError(f"dedup must use hash index, not scan layer {layer}")

    gw = MemoryGateway.__new__(MemoryGateway)
    gw._store = Store()

    assert gw._find_existing_by_hash(_capture_content_hash("duplicate")) == (
        "existing-id",
        "project",
    )


def test_async_capture_enqueue_writes_durable_job_without_gateway_capture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.capture_queue import enqueue_capture
    import core.capture_queue as capture_queue

    def fail_capture(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("enqueue must not materialize capture synchronously")

    monkeypatch.setattr(capture_queue.MemoryGateway, "capture", fail_capture, raising=False)

    queued = enqueue_capture(
        repo_root=tmp_path,
        content="queued content",
        layer="session",
        tags=["fast"],
        session_id="s1",
        no_classify=True,
    )

    job_path = tmp_path / ".agent" / "state" / "capture-queue" / "pending" / f"{queued.item_id}.json"
    assert queued.status == "queued"
    assert job_path.exists()
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    assert payload["item_id"] == queued.item_id
    assert payload["content"] == "queued content"
    assert payload["no_classify"] is True


def test_capture_worker_materializes_pending_jobs(tmp_path: Path, monkeypatch) -> None:
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    captured: list[dict[str, Any]] = []

    class FakeGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            captured.append(kwargs)
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FakeGateway)

    queued = enqueue_capture(
        repo_root=tmp_path,
        content="materialize me",
        layer="project",
        tags=["worker"],
        session_id="s2",
        no_classify=True,
    )
    result = process_pending_captures(repo_root=tmp_path)

    assert result.processed == 1
    assert captured == [
        {
            "content": "materialize me",
            "layer": "project",
            "tags": ["worker"],
            "quality_score": 0.8,
            "run_id": None,
            "session_id": "s2",
            "item_id": queued.item_id,
            "no_classify": True,
        }
    ]
    assert not (tmp_path / ".agent" / "state" / "capture-queue" / "pending" / f"{queued.item_id}.json").exists()
    assert (tmp_path / ".agent" / "state" / "capture-queue" / "done" / f"{queued.item_id}.json").exists()


def test_capture_worker_claims_jobs_before_capture_to_prevent_duplicate_processing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    captured: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class SlowGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            captured.append(kwargs["item_id"])
            entered.set()
            release.wait(timeout=5)
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", SlowGateway)

    queued = enqueue_capture(repo_root=tmp_path, content="claim once", layer="session")
    first = threading.Thread(target=process_pending_captures, kwargs={"repo_root": tmp_path})
    second = threading.Thread(target=process_pending_captures, kwargs={"repo_root": tmp_path})

    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert captured == [queued.item_id]


def test_capture_worker_limit_counts_failed_jobs(tmp_path: Path, monkeypatch) -> None:
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    class FailingGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **_kwargs: Any) -> str:
            raise RuntimeError("write failed")

    monkeypatch.setattr(capture_queue, "MemoryGateway", FailingGateway)
    enqueue_capture(repo_root=tmp_path, content="first", layer="session")
    enqueue_capture(repo_root=tmp_path, content="second", layer="session")

    result = process_pending_captures(repo_root=tmp_path, limit=1)

    queue_root = tmp_path / ".agent" / "state" / "capture-queue"
    assert result.failed == 1
    assert len(list((queue_root / "failed").glob("*.json"))) == 1
    assert len(list((queue_root / "pending").glob("*.json"))) == 1


def test_obsidian_hash_index_drops_stale_hash_when_content_changes(tmp_path: Path, monkeypatch) -> None:
    from core.gateway import _capture_content_hash
    from core.obsidian import ObsidianBackend

    monkeypatch.setenv("MNEMOS_HASH_INDEX_CACHE_DIR", str(tmp_path / "hash-cache"))
    backend = ObsidianBackend(vault_path=str(tmp_path / "vault"))
    path = backend.write(
        layer="project",
        item_id="hash-update-001",
        content="old content",
        metadata={"id": "hash-update-001", "layer": "project"},
    )

    backend.update(str(path), content="new content")

    assert backend.find_by_content_hash(_capture_content_hash("old content")) is None
    assert backend.find_by_content_hash(_capture_content_hash("new content")) == (
        "hash-update-001",
        "project",
    )


def test_obsidian_hash_index_stays_outside_vault_worktree(tmp_path: Path, monkeypatch) -> None:
    from core.gateway import _capture_content_hash
    from core.obsidian import ObsidianBackend

    monkeypatch.setenv("MNEMOS_HASH_INDEX_CACHE_DIR", str(tmp_path / "hash-cache"))
    vault = tmp_path / "vault"
    backend = ObsidianBackend(vault_path=str(vault))
    backend.write(
        layer="project",
        item_id="hash-cache-location-001",
        content="cache location content",
        metadata={"id": "hash-cache-location-001", "layer": "project"},
    )

    assert not (vault / ".mnemos" / "hash-index.sqlite3").exists()
    assert backend.find_by_content_hash(_capture_content_hash("cache location content")) == (
        "hash-cache-location-001",
        "project",
    )


def test_obsidian_hash_index_follows_promoted_item_layer_and_path(tmp_path: Path, monkeypatch) -> None:
    from core.gateway import _capture_content_hash
    from core.obsidian import ObsidianBackend

    monkeypatch.setenv("MNEMOS_HASH_INDEX_CACHE_DIR", str(tmp_path / "hash-cache"))
    backend = ObsidianBackend(vault_path=str(tmp_path / "vault"))
    backend.write(
        layer="project",
        item_id="hash-promote-001",
        content="promoted hash content",
        metadata={"id": "hash-promote-001", "layer": "project"},
    )

    backend.promote("hash-promote-001", "global")

    def fail_scan(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("promoted item should keep the id path cache current")

    monkeypatch.setattr(backend, "_find_path", fail_scan)

    assert backend.find_by_content_hash(_capture_content_hash("promoted hash content")) == (
        "hash-promote-001",
        "global",
    )
    assert backend.read("hash-promote-001")["layer"] == "global"


def test_cli_capture_async_reports_queued_status(tmp_path: Path, monkeypatch) -> None:
    from core.cli import cli

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))

    result = CliRunner().invoke(
        cli,
        ["capture", "queued from cli", "--async", "--no-classify", "--layer", "session"],
    )

    assert result.exit_code == 0, result.output
    assert "queued:" in result.output
    assert "captured:" not in result.output
    queue_dir = tmp_path / ".agent" / "state" / "capture-queue" / "pending"
    assert len(list(queue_dir.glob("*.json"))) == 1
