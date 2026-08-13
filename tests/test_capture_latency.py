"""Regression tests for latency-sensitive capture paths."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate_capture_worker_process(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Keep unit tests from starting a detached process outside their fixture."""
    import core.capture_queue as capture_queue

    original = capture_queue.start_capture_worker
    monkeypatch.setattr(
        capture_queue,
        "start_capture_worker",
        lambda _repo_root: SimpleNamespace(started=True, pid=1234, error_code=None),
        raising=False,
    )
    return original


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

    job_path = queued.path
    assert queued.status == "queued"
    assert job_path.exists()
    payload = json.loads(job_path.read_text(encoding="utf-8"))
    assert payload["item_id"] == queued.item_id
    assert payload["content"] == "queued content"
    assert payload["no_classify"] is True


def test_boundary_case_async_queue_path_does_not_trust_item_id(
    tmp_path: Path,
) -> None:
    """A caller-controlled item ID must never become a queue filesystem path."""
    from core.capture_queue import enqueue_capture

    outside_queue = tmp_path / "outside-queue"
    outside_queue.mkdir()
    item_id = str(outside_queue / "escaped")

    queued = enqueue_capture(
        repo_root=tmp_path,
        content="path isolation",
        layer="session",
        item_id=item_id,
    )

    pending_dir = tmp_path / ".agent" / "state" / "capture-queue" / "pending"
    assert queued.item_id == item_id
    assert queued.path.parent == pending_dir
    assert queued.path.exists()
    assert not (outside_queue / "escaped.json").exists()


def test_boundary_case_async_queue_preserves_jobs_with_same_item_id(
    tmp_path: Path,
) -> None:
    """Logical IDs identify memories, while independent job IDs identify queue files."""
    from core.capture_queue import enqueue_capture

    first = enqueue_capture(
        repo_root=tmp_path,
        content="first payload",
        layer="session",
        item_id="shared-item",
    )
    second = enqueue_capture(
        repo_root=tmp_path,
        content="second payload",
        layer="session",
        item_id="shared-item",
    )

    assert first.path != second.path
    assert first.path.exists()
    assert second.path.exists()
    assert len(list(first.path.parent.glob("*.json"))) == 2


def test_success_case_async_enqueue_starts_worker_after_durable_job_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-001: removing post-persist worker startup must break this test."""
    from core.capture_queue import enqueue_capture
    import core.capture_queue as capture_queue

    # given
    launch_observation: dict[str, Any] = {}

    def observe_launch(repo_root: str | Path) -> SimpleNamespace:
        pending = Path(repo_root) / ".agent" / "state" / "capture-queue" / "pending"
        launch_observation["durable_jobs"] = len(list(pending.glob("*.json")))
        return SimpleNamespace(started=True, pid=4321, error_code=None)

    monkeypatch.setattr(capture_queue, "start_capture_worker", observe_launch)
    sut = enqueue_capture

    # when
    queued = sut(repo_root=tmp_path, content="start worker", layer="session")

    # then
    assert launch_observation == {"durable_jobs": 1}
    assert queued.worker_started is True
    assert queued.worker_pid == 4321
    assert queued.worker_error_code is None


def test_boundary_case_async_enqueue_preserves_job_when_worker_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-002: launcher failure must never roll back a durable capture job."""
    from core.capture_queue import enqueue_capture
    import core.capture_queue as capture_queue

    # given
    def fail_launch(_repo_root: str | Path) -> None:
        raise OSError("process unavailable")

    monkeypatch.setattr(capture_queue, "start_capture_worker", fail_launch)
    sut = enqueue_capture

    # when
    queued = sut(repo_root=tmp_path, content="keep durable", layer="session")

    # then
    assert queued.status == "queued"
    assert queued.path.exists()
    assert queued.worker_started is False
    assert queued.worker_pid is None
    assert queued.worker_error_code == "worker_start_failed"


def test_boundary_case_capture_worker_command_uses_bounded_fallbacks(monkeypatch) -> None:
    """Detached startup has deterministic installed and source-only argv forms."""
    import core.capture_queue as capture_queue

    # given
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(capture_queue.shutil, "which", lambda _name: "/opt/bin/mnemos")

    # when
    installed = capture_queue._capture_worker_command()
    monkeypatch.setattr(capture_queue.shutil, "which", lambda _name: None)
    source_only = capture_queue._capture_worker_command()

    # then
    assert installed == ["/opt/bin/mnemos", "capture-worker"]
    assert source_only[:2] == [capture_queue.sys.executable, "-c"]
    assert "worker_entrypoint" in source_only[2]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProcessLookupError(), False),
        (PermissionError(), True),
        (OSError(), False),
    ],
)
def test_boundary_case_capture_pid_probe_normalizes_platform_errors(
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError,
    expected: bool,
) -> None:
    """Worker health maps OS process-probe errors to stable states."""
    import core.capture_queue as capture_queue

    # given
    def fail_probe(_pid: int, _signal: int) -> None:
        raise failure

    monkeypatch.setattr(capture_queue.os, "kill", fail_probe)

    # when
    sut = capture_queue._pid_is_running(1234)

    # then
    assert sut is expected
    assert capture_queue._pid_is_running(0) is False


def test_failure_case_capture_worker_launch_failure_is_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_capture_worker_process,
) -> None:
    """A Popen failure returns a bounded code and durable status."""
    import core.capture_queue as capture_queue

    # given
    monkeypatch.setattr(
        capture_queue.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")),
    )
    sut = _isolate_capture_worker_process

    # when
    result = sut(tmp_path)
    diagnostics = capture_queue.get_capture_queue_diagnostics(repo_root=tmp_path)

    # then
    assert result == capture_queue.WorkerStartResult(
        started=False,
        error_code="worker_start_failed",
    )
    assert diagnostics["worker"] == {
        "state": "launch_failed",
        "pid": None,
        "error_code": "worker_start_failed",
    }


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
    assert not queued.path.exists()
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


def test_success_case_capture_worker_single_flight_prevents_duplicate_processing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """TC-003: removing the worker lease must allow duplicate worker entry."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    captured: list[str] = []
    results: list[Any] = []
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

    def run_worker() -> None:
        results.append(process_pending_captures(repo_root=tmp_path))

    first = threading.Thread(target=run_worker)
    second = threading.Thread(target=run_worker)

    # when
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    # then
    assert not first.is_alive()
    assert not second.is_alive()
    assert captured == [queued.item_id]
    assert sorted(result.skipped_locked for result in results) == [False, True]


def test_failure_case_capture_worker_limit_counts_terminal_failed_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A terminal failure must consume one limit slot and isolate the job."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    class FailingGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **_kwargs: Any) -> str:
            raise RuntimeError("write failed")

    monkeypatch.setattr(capture_queue, "MemoryGateway", FailingGateway)
    enqueue_capture(repo_root=tmp_path, content="first", layer="session")
    enqueue_capture(repo_root=tmp_path, content="second", layer="session")

    sut = process_pending_captures

    # when
    result = sut(repo_root=tmp_path, limit=1, max_attempts=1)

    # then
    queue_root = tmp_path / ".agent" / "state" / "capture-queue"
    assert result.failed == 1
    assert len(list((queue_root / "failed").glob("*.json"))) == 1
    assert len(list((queue_root / "pending").glob("*.json"))) == 1


def test_success_case_capture_worker_recovers_abandoned_processing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-005: an abandoned claim must not remain permanently unsearchable."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    captured: list[str] = []

    class FakeGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            captured.append(kwargs["item_id"])
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FakeGateway)
    queued = enqueue_capture(repo_root=tmp_path, content="recover me", layer="session")
    processing_dir = queued.path.parent.parent / "processing"
    processing_dir.mkdir(parents=True)
    queued.path.replace(processing_dir / queued.path.name)
    lock_dir = queued.path.parent.parent / "worker.lock"
    lock_dir.mkdir()
    (lock_dir / "owner.json").write_text(
        json.dumps({"pid": os.getpid() + 10_000_000, "token": "dead-worker"}),
        encoding="utf-8",
    )
    sut = process_pending_captures

    # when
    result = sut(repo_root=tmp_path)

    # then
    assert result.recovered == 1
    assert result.processed == 1
    assert captured == [queued.item_id]
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


def test_boundary_case_capture_worker_does_not_reclaim_fresh_legacy_processing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-005: a lockless in-flight legacy worker must receive an age grace period."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    class UnexpectedGateway:
        def __init__(self, repo_root: str) -> None:
            raise AssertionError(f"fresh processing job must not be reclaimed: {repo_root}")

    monkeypatch.setattr(capture_queue, "MemoryGateway", UnexpectedGateway)
    queued = enqueue_capture(repo_root=tmp_path, content="legacy active", layer="session")
    processing_dir = queued.path.parent.parent / "processing"
    processing_dir.mkdir(parents=True)
    processing_path = processing_dir / queued.path.name
    queued.path.replace(processing_path)
    sut = process_pending_captures

    # when
    result = sut(repo_root=tmp_path)

    # then
    assert result.recovered == 0
    assert result.processed == 0
    assert processing_path.exists()


def test_success_case_capture_worker_retries_transient_failure_without_losing_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-007: moving the first transient failure to terminal failed must break this test."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    attempts = 0

    class FlakyGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FlakyGateway)
    queued = enqueue_capture(repo_root=tmp_path, content="retry me", layer="session")
    sut = process_pending_captures

    # when
    first = sut(repo_root=tmp_path, max_attempts=2)
    second = sut(repo_root=tmp_path, max_attempts=2)

    # then
    assert first.retried == 1
    assert first.failed == 0
    assert second.processed == 1
    assert attempts == 2
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


def test_success_case_capture_worker_resumes_materialization_after_store_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A derived-index failure must resume after canonical storage, not dedup away."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    from core.fts import FTSIndex
    from core.gateway import MemoryGateway

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    source_policy = Path(__file__).parents[1] / "repo" / "wiki" / "policy.yaml"
    (wiki / "policy.yaml").write_text(
        source_policy.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    original_index_item = FTSIndex.index_item
    attempts = 0

    def fail_first_index(self: FTSIndex, *args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("fts temporarily unavailable")
        original_index_item(self, *args, **kwargs)

    monkeypatch.setattr(FTSIndex, "index_item", fail_first_index)
    queued = enqueue_capture(
        repo_root=tmp_path,
        content="materialization recovery sentinel",
        layer="session",
        no_classify=True,
    )

    first = process_pending_captures(repo_root=tmp_path, max_attempts=2)
    retry_payload = json.loads(queued.path.read_text(encoding="utf-8"))
    second = process_pending_captures(repo_root=tmp_path, max_attempts=2)

    assert first.retried == 1
    assert retry_payload["completed_phases"] == ["store_write"]
    assert second.processed == 1
    assert attempts == 2

    gateway = MemoryGateway(repo_root=str(tmp_path))
    assert [
        result["item_id"]
        for result in gateway._fts.search("materialization recovery sentinel")
    ] == [queued.item_id]
    audit_entries = [
        json.loads(line)
        for line in (wiki / "log.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [
        entry
        for entry in audit_entries
        if entry["operation"] == "capture" and entry["item_id"] == queued.item_id
    ]


def test_success_case_capture_worker_replays_store_when_phase_journal_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash window after storage must not let persistent dedup skip recovery."""
    import core.capture_queue as capture_queue
    from core.gateway import MemoryGateway

    wiki = tmp_path / "wiki"
    wiki.mkdir()
    source_policy = Path(__file__).parents[1] / "repo" / "wiki" / "policy.yaml"
    (wiki / "policy.yaml").write_text(
        source_policy.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    original_record = MemoryGateway._record_capture_phase
    failed_once = False

    def fail_first_store_phase(
        phase: str,
        *,
        completed_phases: set[str],
        callback: Any,
    ) -> None:
        nonlocal failed_once
        if phase == "store_write" and not failed_once:
            failed_once = True
            raise RuntimeError("phase journal unavailable")
        original_record(
            phase,
            completed_phases=completed_phases,
            callback=callback,
        )

    monkeypatch.setattr(
        MemoryGateway,
        "_record_capture_phase",
        staticmethod(fail_first_store_phase),
    )
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="store replay sentinel",
        layer="session",
        no_classify=True,
    )

    first = capture_queue.process_pending_captures(
        repo_root=tmp_path,
        max_attempts=2,
    )
    second = capture_queue.process_pending_captures(
        repo_root=tmp_path,
        max_attempts=2,
    )

    gateway = MemoryGateway(repo_root=str(tmp_path))
    assert first.retried == 1
    assert second.processed == 1
    assert [
        result["item_id"] for result in gateway._fts.search("store replay sentinel")
    ] == [queued.item_id]


def test_success_case_capture_worker_drain_retries_until_job_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-007: the detached worker must consume its own retry without a new enqueue."""
    import core.capture_queue as capture_queue

    # given
    attempts = 0

    class FlakyGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FlakyGateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="drain retry",
        layer="session",
    )
    sut = capture_queue.drain_pending_captures

    # when
    result = sut(repo_root=tmp_path, max_attempts=2)

    # then
    assert result.processed == 1
    assert result.retried == 1
    assert result.failed == 0
    assert attempts == 2
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


def test_success_case_capture_worker_drain_consumes_job_enqueued_during_active_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-003: a job queued behind an active lease must not remain pending."""
    import core.capture_queue as capture_queue

    # given
    captured: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    class SlowFirstGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            captured.append(kwargs["item_id"])
            if len(captured) == 1:
                entered.set()
                release.wait(timeout=5)
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", SlowFirstGateway)
    first = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="first active",
        layer="session",
    )
    worker = threading.Thread(
        target=capture_queue.drain_pending_captures,
        kwargs={"repo_root": tmp_path},
    )
    sut = capture_queue.enqueue_capture

    # when
    worker.start()
    assert entered.wait(timeout=5)
    second = sut(repo_root=tmp_path, content="queued behind lock", layer="session")
    release.set()
    worker.join(timeout=5)

    # then
    assert not worker.is_alive()
    assert captured == [first.item_id, second.item_id]
    assert not second.path.exists()
    assert (second.path.parent.parent / "done" / second.path.name).exists()


def test_success_case_capture_worker_retries_gateway_initialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-002: gateway startup failure must follow the durable retry path."""
    import core.capture_queue as capture_queue

    # given
    initializations = 0

    class FlakyGateway:
        def __init__(self, repo_root: str) -> None:
            nonlocal initializations
            initializations += 1
            if initializations == 1:
                raise RuntimeError("gateway unavailable")
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FlakyGateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="retry gateway init",
        layer="session",
    )
    sut = capture_queue.drain_pending_captures

    # when
    result = sut(repo_root=tmp_path, max_attempts=2)

    # then
    assert result.retried == 1
    assert result.processed == 1
    assert initializations == 2
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


def test_failure_case_capture_worker_isolates_malformed_job_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-006: one malformed file must not prevent a later valid capture."""
    from core.capture_queue import enqueue_capture, process_pending_captures
    import core.capture_queue as capture_queue

    # given
    captured: list[str] = []

    class FakeGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            captured.append(kwargs["item_id"])
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", FakeGateway)
    queue_root = tmp_path / ".agent" / "state" / "capture-queue"
    pending_dir = queue_root / "pending"
    pending_dir.mkdir(parents=True)
    (pending_dir / "000-malformed.json").write_text("{not-json", encoding="utf-8")
    queued = enqueue_capture(repo_root=tmp_path, content="still runs", layer="session")
    sut = process_pending_captures

    # when
    result = sut(repo_root=tmp_path, max_attempts=2)

    # then
    assert result.failed == 1
    assert result.processed == 1
    assert captured == [queued.item_id]
    failed_payload = json.loads((queue_root / "failed" / "000-malformed.json").read_text(encoding="utf-8"))
    assert failed_payload["error_type"] == "JSONDecodeError"
    assert "content" not in failed_payload


def test_failure_case_malformed_capture_receipt_preserves_logical_item_id(
    tmp_path: Path,
) -> None:
    """A schema-invalid job remains discoverable by hard-delete cleanup."""
    import core.capture_queue as capture_queue

    pending = capture_queue._queue_root(tmp_path) / "pending"
    pending.mkdir(parents=True)
    pending_path = pending / "bad-job.json"
    pending_path.write_text(
        json.dumps({"item_id": "victim", "content": "", "layer": "session"}),
        encoding="utf-8",
    )

    result = capture_queue.process_pending_captures(repo_root=tmp_path)

    failed_path = pending.parent / "failed" / pending_path.name
    receipt = json.loads(failed_path.read_text(encoding="utf-8"))
    assert result.failed == 1
    assert receipt["item_id"] == "victim"
    assert "content" not in receipt
    assert capture_queue.purge_capture_jobs(
        repo_root=tmp_path,
        item_id="victim",
    ) == 1
    assert not failed_path.exists()


def test_security_case_terminal_capture_receipts_do_not_retain_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal queue states retain operational metadata but drop memory text."""
    import core.capture_queue as capture_queue

    class Gateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            if kwargs["content"] == "failed secret":
                raise RuntimeError("terminal failure")
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", Gateway)
    succeeded = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="completed secret",
        layer="session",
    )
    failed = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="failed secret",
        layer="session",
    )

    result = capture_queue.process_pending_captures(
        repo_root=tmp_path,
        max_attempts=1,
    )

    root = succeeded.path.parent.parent
    done_receipt = json.loads(
        (root / "done" / succeeded.path.name).read_text(encoding="utf-8")
    )
    failed_receipt = json.loads(
        (root / "failed" / failed.path.name).read_text(encoding="utf-8")
    )
    assert result.processed == 1
    assert result.failed == 1
    assert done_receipt["status"] == "captured"
    assert failed_receipt["status"] == "failed"
    assert done_receipt["item_id"] == succeeded.item_id
    assert failed_receipt["item_id"] == failed.item_id
    assert failed_receipt["error_type"] == "RuntimeError"
    assert "content" not in done_receipt
    assert "content" not in failed_receipt


@pytest.mark.parametrize("capture_fails", [False, True])
def test_concurrency_case_hard_delete_prevents_terminal_receipt_recreation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_fails: bool,
) -> None:
    """An active worker cannot recreate a receipt after its item was purged."""
    import core.capture_queue as capture_queue

    entered = threading.Event()
    release = threading.Event()
    deleted: list[str] = []

    class SlowGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=5)
            if capture_fails:
                raise RuntimeError("capture failed after purge")
            return kwargs["item_id"]

        def delete(self, item_id: str) -> None:
            deleted.append(item_id)

    monkeypatch.setattr(capture_queue, "MemoryGateway", SlowGateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="purged while active",
        layer="session",
        item_id="victim",
    )
    worker = threading.Thread(
        target=capture_queue.process_pending_captures,
        kwargs={"repo_root": tmp_path},
    )

    worker.start()
    assert entered.wait(timeout=5)
    capture_queue.purge_capture_jobs(repo_root=tmp_path, item_id="victim")
    release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert not queued.path.exists()
    assert not (queued.path.parent.parent / "processing" / queued.path.name).exists()
    assert not (queued.path.parent.parent / "done" / queued.path.name).exists()
    assert not (queued.path.parent.parent / "failed" / queued.path.name).exists()
    assert deleted == ["victim"]


def test_concurrency_case_purge_after_failure_check_removes_recreated_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge between failure check and retry move still wins the race."""
    import core.capture_queue as capture_queue

    class FailingGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **_kwargs: Any) -> None:
            raise RuntimeError("retry race")

        def delete(self, _item_id: str) -> None:
            return None

    monkeypatch.setattr(capture_queue, "MemoryGateway", FailingGateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="retry must stay deleted",
        layer="session",
        item_id="victim",
    )
    original_check = capture_queue._job_was_purged
    checks = 0

    def purge_after_first_check(root: Path, payload: dict[str, Any]) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            capture_queue.purge_capture_jobs(repo_root=tmp_path, item_id="victim")
            return False
        return original_check(root, payload)

    monkeypatch.setattr(capture_queue, "_job_was_purged", purge_after_first_check)

    result = capture_queue.process_pending_captures(repo_root=tmp_path)

    queue_root = queued.path.parent.parent
    assert result.retried == 0
    assert not any((queue_root / state / queued.path.name).exists() for state in [
        "pending",
        "processing",
        "done",
        "failed",
    ])


def test_boundary_case_capture_queued_after_delete_cutoff_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delete tombstone cancels old jobs without banning a future same-ID job."""
    import core.capture_queue as capture_queue

    class Gateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", Gateway)
    capture_queue.purge_capture_jobs(repo_root=tmp_path, item_id="reusable")
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="new generation",
        layer="session",
        item_id="reusable",
    )

    result = capture_queue.process_pending_captures(repo_root=tmp_path)

    assert result.processed == 1
    assert (queued.path.parent.parent / "done" / queued.path.name).exists()


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ([], "TypeError"),
        ({"item_id": "missing-content", "content": ""}, "ValueError"),
        ({"item_id": "", "content": "valid content"}, "ValueError"),
    ],
)
def test_failure_case_capture_worker_isolates_invalid_payload_shapes(
    tmp_path: Path,
    payload: Any,
    expected_error: str,
) -> None:
    """Schema-invalid jobs become content-free failures without gateway startup."""
    import core.capture_queue as capture_queue

    # given
    pending = tmp_path / ".agent" / "state" / "capture-queue" / "pending"
    pending.mkdir(parents=True)
    (pending / "invalid.json").write_text(json.dumps(payload), encoding="utf-8")

    # when
    sut = capture_queue.process_pending_captures(repo_root=tmp_path)

    # then
    assert sut.failed == 1
    failed = json.loads(
        (pending.parent / "failed" / "invalid.json").read_text(encoding="utf-8")
    )
    assert failed["error_type"] == expected_error
    assert "content" not in failed


def test_failure_case_capture_worker_normalizes_invalid_retry_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt retry metadata cannot bypass the terminal-attempt budget."""
    import core.capture_queue as capture_queue

    # given
    class FailingGateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **_kwargs: Any) -> None:
            raise RuntimeError("private")

    monkeypatch.setattr(capture_queue, "MemoryGateway", FailingGateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="retry metadata",
        layer="session",
    )
    payload = json.loads(queued.path.read_text(encoding="utf-8"))
    payload["attempts"] = "invalid"
    queued.path.write_text(json.dumps(payload), encoding="utf-8")

    # when
    sut = capture_queue.process_pending_captures(
        repo_root=tmp_path,
        max_attempts=1,
    )

    # then
    assert sut.failed == 1
    failed = json.loads(
        (queued.path.parent.parent / "failed" / queued.path.name).read_text()
    )
    assert failed["attempts"] == 1
    assert failed["error_type"] == "RuntimeError"


def test_boundary_case_capture_worker_normalizes_invalid_success_counter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt optional retry metadata cannot prevent a successful receipt."""
    import core.capture_queue as capture_queue

    class Gateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            return kwargs["item_id"]

    monkeypatch.setattr(capture_queue, "MemoryGateway", Gateway)
    queued = capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="valid capture",
        layer="session",
    )
    payload = json.loads(queued.path.read_text(encoding="utf-8"))
    payload["attempts"] = "invalid"
    queued.path.write_text(json.dumps(payload), encoding="utf-8")

    result = capture_queue.process_pending_captures(repo_root=tmp_path)

    receipt = json.loads(
        (queued.path.parent.parent / "done" / queued.path.name).read_text()
    )
    assert result.processed == 1
    assert receipt["attempts"] == 0


def test_failure_case_capture_worker_records_unexpected_queue_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue-level crashes write bounded status and release single-flight state."""
    import core.capture_queue as capture_queue

    # given
    monkeypatch.setattr(
        capture_queue,
        "_recover_abandoned_jobs",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )

    # when
    with pytest.raises(RuntimeError):
        capture_queue.process_pending_captures(repo_root=tmp_path)
    diagnostics = capture_queue.get_capture_queue_diagnostics(repo_root=tmp_path)

    # then
    assert diagnostics["worker"] == {
        "state": "failed",
        "pid": os.getpid(),
        "error_code": "RuntimeError",
    }
    assert not (capture_queue._queue_root(tmp_path) / "worker.lock").exists()


def test_success_case_capture_queue_diagnostics_report_depth_age_and_worker_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-008: omitting any queue state or oldest age must break this test."""
    from core.capture_queue import enqueue_capture
    import core.capture_queue as capture_queue

    # given
    queued = enqueue_capture(repo_root=tmp_path, content="diagnose", layer="session")
    queue_root = queued.path.parent.parent
    processing_dir = queue_root / "processing"
    failed_dir = queue_root / "failed"
    processing_dir.mkdir(parents=True)
    failed_dir.mkdir(parents=True)
    (processing_dir / "processing.json").write_text("{}", encoding="utf-8")
    (failed_dir / "failed.json").write_text("{}", encoding="utf-8")
    os.utime(queued.path, (900.0, 900.0))
    monkeypatch.setattr(capture_queue.time, "time", lambda: 1000.0)
    sut = capture_queue.get_capture_queue_diagnostics

    # when
    diagnostics = sut(repo_root=tmp_path)

    # then
    assert diagnostics == {
        "pending": 1,
        "processing": 1,
        "failed": 1,
        "done": 0,
        "sync_pending": 0,
        "oldest_pending_age_seconds": 100.0,
        "worker": {"state": "idle", "pid": None, "error_code": None},
    }


def test_boundary_case_capture_diagnostics_identify_stale_lock_and_status(
    tmp_path: Path,
) -> None:
    """Malformed lock ownership and orphaned startup remain visible as stale."""
    import core.capture_queue as capture_queue

    # given
    root = capture_queue._queue_root(tmp_path)
    lock_path = root / "worker.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "owner.json").write_text("invalid", encoding="utf-8")

    # when
    malformed_lock = capture_queue.get_capture_queue_diagnostics(repo_root=tmp_path)
    for path in lock_path.iterdir():
        path.unlink()
    lock_path.rmdir()
    capture_queue._write_worker_status(
        root,
        state="starting",
        pid=None,
        error_code="worker_start_failed",
    )
    orphaned_status = capture_queue.get_capture_queue_diagnostics(repo_root=tmp_path)

    # then
    assert malformed_lock["worker"]["state"] == "stale"
    assert orphaned_status["worker"] == {
        "state": "stale",
        "pid": None,
        "error_code": "worker_start_failed",
    }


def test_boundary_case_capture_drain_refuses_a_live_worker_lease(
    tmp_path: Path,
) -> None:
    """A second drain cannot enter while the current process owns the queue."""
    import core.capture_queue as capture_queue

    # given
    root = capture_queue._queue_root(tmp_path)
    lease = capture_queue._acquire_worker_lease(root)
    assert lease is not None

    # when
    sut = capture_queue.drain_pending_captures(repo_root=tmp_path)

    # then
    assert sut.skipped_locked is True
    capture_queue._release_worker_lease(lease)


def test_boundary_case_finishing_capture_worker_does_not_clobber_new_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successor worker owns status once the prior lease is released."""
    import core.capture_queue as capture_queue

    # given
    class Gateway:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def capture(self, **kwargs: Any) -> str:
            return str(kwargs["item_id"])

    monkeypatch.setattr(capture_queue, "MemoryGateway", Gateway)
    capture_queue.enqueue_capture(
        repo_root=tmp_path,
        content="status ownership",
        layer="session",
    )

    def simulate_successor(_lease: Any) -> None:
        capture_queue._write_worker_status(
            capture_queue._queue_root(tmp_path),
            state="running",
            pid=os.getpid(),
        )

    monkeypatch.setattr(capture_queue, "_release_worker_lease", simulate_successor)

    # when
    capture_queue.process_pending_captures(repo_root=tmp_path)
    status = json.loads(
        (capture_queue._queue_root(tmp_path) / "worker-status.json").read_text()
    )

    # then
    assert status["state"] == "running"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("success", 0), ("failed_jobs", 1), ("exception", 1)],
)
def test_boundary_case_capture_worker_entrypoint_normalizes_terminal_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected: int,
) -> None:
    """Detached capture workers expose deterministic exit codes and status."""
    import core.capture_queue as capture_queue

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    if outcome == "success":
        monkeypatch.setattr(
            capture_queue,
            "drain_pending_captures",
            lambda **_kwargs: capture_queue.CaptureQueueResult(processed=1, failed=0),
        )
    elif outcome == "failed_jobs":
        monkeypatch.setattr(
            capture_queue,
            "drain_pending_captures",
            lambda **_kwargs: capture_queue.CaptureQueueResult(processed=0, failed=1),
        )
    else:
        monkeypatch.setattr(
            capture_queue,
            "drain_pending_captures",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
        )

    # when
    sut = capture_queue.worker_entrypoint()

    # then
    assert sut == expected
    if outcome == "exception":
        diagnostics = capture_queue.get_capture_queue_diagnostics(repo_root=tmp_path)
        assert diagnostics["worker"]["error_code"] == "RuntimeError"


def test_success_case_capture_worker_status_cli_returns_machine_readable_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-009: queue diagnostics must remain available without draining jobs."""
    from core.capture_queue import enqueue_capture
    from core.cli import cli

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    queued = enqueue_capture(repo_root=tmp_path, content="leave pending", layer="session")
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["capture-worker", "--status", "--json"])

    # then
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["queue"]["pending"] == 1
    assert payload["queue"]["worker"]["state"] == "idle"
    assert queued.path.exists()


def test_success_case_capture_worker_cli_text_modes_report_bounded_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human queue status and bounded foreground processing stay script-safe."""
    from core.cli import cli

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    sut = CliRunner()

    # when
    status = sut.invoke(cli, ["capture-worker", "--status"])
    process = sut.invoke(cli, ["capture-worker", "--limit", "0"])

    # then
    assert status.exit_code == 0
    assert status.output == (
        "pending=0 processing=0 failed=0 sync_pending=0 worker=idle\n"
    )
    assert process.exit_code == 0
    assert process.output == "processed=0 failed=0 retried=0 recovered=0\n"


def test_success_case_cli_async_json_reports_worker_start_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-009: removing additive worker diagnostics must break this test."""
    from core.cli import cli

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    sut = CliRunner()

    # when
    result = sut.invoke(
        cli,
        ["capture", "queued json", "--async", "--json", "--layer", "session"],
    )

    # then
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued"
    assert payload["worker"] == {
        "started": True,
        "pid": 1234,
        "error_code": None,
    }


def test_success_case_cli_capture_json_includes_phase_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-010: synchronous capture JSON must expose privacy-safe timing evidence."""
    import core.cli as cli_module

    # given
    expected_diagnostics = {
        "duration_ms": 12.0,
        "phases": [{"name": "store_write", "duration_ms": 10.0}],
        "store": {"enabled": True, "phases": []},
    }

    class FakeStore:
        last_sync_failure = None

        def read(self, item_id: str) -> dict[str, Any]:
            return {"id": item_id, "content": "captured", "layer": "session"}

    class FakeGateway:
        last_capture_was_duplicate = False
        last_capture_diagnostics = expected_diagnostics
        _store = FakeStore()

        def capture(self, **_kwargs: Any) -> str:
            return "timed-id"

    monkeypatch.setattr(cli_module, "_get_gateway", lambda: FakeGateway())
    sut = CliRunner()

    # when
    result = sut.invoke(
        cli_module.cli,
        ["capture", "timed", "--json", "--layer", "session", "--no-classify"],
    )

    # then
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capture_diagnostics"] == expected_diagnostics


def test_success_case_fresh_root_async_capture_materializes_without_manual_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_capture_worker_process: Any,
) -> None:
    """TC-001: the real detached process must drain a fresh durable queue."""
    from core.capture_queue import enqueue_capture, get_capture_queue_diagnostics
    from core.gateway import MemoryGateway
    import core.capture_queue as capture_queue

    # given
    wiki = tmp_path / "wiki"
    wiki.mkdir(parents=True)
    source_policy = Path(__file__).parents[1] / "repo" / "wiki" / "policy.yaml"
    (wiki / "policy.yaml").write_text(source_policy.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(
        capture_queue,
        "start_capture_worker",
        _isolate_capture_worker_process,
    )
    sut = enqueue_capture

    # when
    queued = sut(
        repo_root=tmp_path,
        content="portable fresh worker sentinel",
        layer="session",
        session_id="fresh-e2e",
        no_classify=True,
    )
    done_path = queued.path.parent.parent / "done" / queued.path.name
    deadline = time.monotonic() + 10.0
    while not done_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)

    # then
    assert queued.worker_started is True
    assert done_path.exists(), get_capture_queue_diagnostics(repo_root=tmp_path)
    gateway = MemoryGateway(repo_root=str(tmp_path))
    results = gateway.search("portable fresh worker sentinel", layers=["session"])
    assert any(item.get("item_id") == queued.item_id for item in results)


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
