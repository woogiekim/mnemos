"""Tests for the durable asynchronous QMD refresh queue."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import pytest
import yaml


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str | Path]] = []
        self.on_update = None

    def update_index(self, collections: dict[str, str | Path]) -> dict[str, bool]:
        from core.qmd import QmdCommandError

        self.calls.append(dict(collections))
        if self.on_update is not None:
            callback = self.on_update
            self.on_update = None
            callback()
        if self.fail:
            raise QmdCommandError("update", "nonzero_exit")
        return {"updated": True, "embedded": False}


def _write_fake_qmd_executable(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_QMD_CALLS_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")
print("[]")
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _write_minimal_policy(repo_root: Path) -> None:
    wiki = repo_root / "wiki"
    for directory in ("global", "projects", "entities", "claims", "topics"):
        (wiki / directory).mkdir(parents=True, exist_ok=True)
    (wiki / "policy.yaml").write_text(
        yaml.safe_dump(
            {
                "layers": {
                    "project": {
                        "path_template": "wiki/projects/",
                        "promotes_to": "global",
                        "promotion": {
                            "age_hours": 0,
                            "access_count": 0,
                            "quality_score": 0,
                        },
                    },
                    "global": {
                        "path_template": "wiki/global/",
                        "promotes_to": None,
                        "promotion": {
                            "age_hours": 0,
                            "access_count": 0,
                            "quality_score": 0,
                        },
                    },
                },
                "forget": {"requires_archived": True},
                "archive": {"allowed_stages": ["stored"]},
            }
        ),
        encoding="utf-8",
    )


def test_boundary_case_disabled_qmd_refresh_does_not_create_queue(
    tmp_path: Path,
) -> None:
    """A disabled optional backend must add zero write-path side effects."""
    from core.config import QmdConfig
    from core.qmd_queue import enqueue_qmd_refresh

    # given
    config = QmdConfig(enabled=False)

    # when
    sut = enqueue_qmd_refresh(repo_root=tmp_path, reason="capture", config=config)

    # then
    assert sut.enabled is False
    assert sut.queued is False
    assert not (tmp_path / ".agent" / "state" / "qmd-refresh").exists()


def test_success_case_qmd_refresh_enqueue_is_durable_before_worker_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A worker launch failure must leave a content-free durable pending job."""
    from core.config import QmdConfig
    from core.qmd_queue import WorkerStartResult, enqueue_qmd_refresh

    # given
    monkeypatch.setattr(
        "core.qmd_queue.start_qmd_index_worker",
        lambda _repo_root: WorkerStartResult(
            started=False,
            error_code="worker_start_failed",
        ),
    )

    # when
    sut = enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="capture",
        config=QmdConfig(enabled=True),
    )

    # then
    assert sut.enabled is True
    assert sut.queued is True
    assert sut.worker_started is False
    assert sut.worker_error_code == "worker_start_failed"
    payload = json.loads(sut.job_path.read_text(encoding="utf-8"))
    assert set(payload) == {"job_id", "reason", "queued_at", "attempts"}
    assert payload["reason"] == "capture"


def test_boundary_case_qmd_refresh_reason_cannot_persist_memory_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Unknown caller text is reduced to a bounded operational reason token."""
    from core.config import QmdConfig
    import core.qmd_queue as qmd_queue

    # given
    private_reason = "capture because private customer memory says secret"
    monkeypatch.setattr(
        qmd_queue,
        "start_qmd_index_worker",
        lambda _repo_root: qmd_queue.WorkerStartResult(started=False),
    )

    # when
    sut = qmd_queue.enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason=private_reason,
        config=QmdConfig(enabled=True),
    )

    # then
    payload = json.loads(sut.job_path.read_text(encoding="utf-8"))
    assert payload["reason"] == "other"
    assert private_reason not in json.dumps(payload)


def test_boundary_case_qmd_worker_scrubs_legacy_private_reason_on_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Claiming a legacy job sanitizes the payload before it reaches done."""
    import core.qmd_queue as qmd_queue

    # given
    private_reason = "private memory content from legacy pending work"
    pending = tmp_path / ".agent" / "state" / "qmd-refresh" / "pending"
    pending.mkdir(parents=True)
    (pending / "legacy.json").write_text(
        json.dumps({"job_id": "legacy", "reason": private_reason, "attempts": 0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (
            _FakeAdapter(),
            {"mnemos-project": tmp_path / "vault"},
        ),
    )

    # when
    sut = qmd_queue.process_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut.processed == 1
    payload = json.loads(
        (pending.parent / "done" / "legacy.json").read_text(encoding="utf-8")
    )
    assert payload["reason"] == "other"
    assert private_reason not in json.dumps(payload)


def test_failure_case_qmd_worker_launch_failure_keeps_pending_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Both direct Popen failure and wrapper failure must leave a durable job."""
    from core.config import QmdConfig
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setattr(
        qmd_queue.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot launch")),
    )

    # when
    start = qmd_queue.start_qmd_index_worker(tmp_path)
    monkeypatch.setattr(
        qmd_queue,
        "start_qmd_index_worker",
        lambda _repo_root: (_ for _ in ()).throw(RuntimeError("wrapper failed")),
    )
    queued = qmd_queue.enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="capture",
        config=QmdConfig(enabled=True),
    )

    # then
    assert start == qmd_queue.WorkerStartResult(
        started=False,
        error_code="worker_start_failed",
    )
    assert queued.queued is True
    assert queued.worker_started is False
    assert queued.worker_error_code == "worker_start_failed"
    assert queued.job_path is not None and queued.job_path.exists()


def test_boundary_case_qmd_worker_command_uses_bounded_fallbacks(monkeypatch) -> None:
    """Worker startup must use argv forms for installed and source-only layouts."""
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setattr(Path, "is_file", lambda _path: False)
    monkeypatch.setattr(qmd_queue.shutil, "which", lambda _name: "/opt/bin/mnemos")

    # when
    installed = qmd_queue._qmd_worker_command()
    monkeypatch.setattr(qmd_queue.shutil, "which", lambda _name: None)
    source_only = qmd_queue._qmd_worker_command()

    # then
    assert installed == ["/opt/bin/mnemos", "qmd-index-worker"]
    assert source_only[:2] == [qmd_queue.sys.executable, "-c"]
    assert "worker_entrypoint" in source_only[2]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (ProcessLookupError(), False),
        (PermissionError(), True),
        (OSError(), False),
    ],
)
def test_boundary_case_qmd_pid_probe_normalizes_platform_errors(
    monkeypatch,
    failure: OSError,
    expected: bool,
) -> None:
    """Single-flight health treats OS process-probe outcomes deterministically."""
    import core.qmd_queue as qmd_queue

    # given
    def fail_probe(_pid: int, _signal: int) -> None:
        raise failure

    monkeypatch.setattr(qmd_queue.os, "kill", fail_probe)

    # when
    sut = qmd_queue._pid_is_running(1234)

    # then
    assert sut is expected
    assert qmd_queue._pid_is_running(0) is False


def test_boundary_case_qmd_lock_health_respects_initialization_grace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Fresh partial locks are protected while old or dead locks are reclaimable."""
    import core.qmd_queue as qmd_queue

    # given
    lock_path = tmp_path / "worker.lock"
    lock_path.mkdir()
    (lock_path / "owner.json").write_text("not-json", encoding="utf-8")

    # when
    fresh = qmd_queue._lock_is_abandoned(lock_path)
    old_time = time.time() - qmd_queue._LOCK_INITIALIZATION_GRACE_SECONDS - 1
    os.utime(lock_path, (old_time, old_time))
    old = qmd_queue._lock_is_abandoned(lock_path)
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": 999999999}),
        encoding="utf-8",
    )
    monkeypatch.setattr(qmd_queue, "_pid_is_running", lambda _pid: False)
    dead = qmd_queue._lock_is_abandoned(lock_path)

    # then
    assert fresh is False
    assert old is True
    assert dead is True


def test_success_case_qmd_refresh_coalesces_pending_jobs_into_one_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One full-index refresh should acknowledge every job in its claimed batch."""
    from core.config import QmdConfig
    from core.qmd_queue import (
        WorkerStartResult,
        enqueue_qmd_refresh,
        get_qmd_refresh_diagnostics,
        process_qmd_refresh,
    )

    # given
    monkeypatch.setattr(
        "core.qmd_queue.start_qmd_index_worker",
        lambda _repo_root: WorkerStartResult(started=False),
    )
    config = QmdConfig(enabled=True)
    enqueue_qmd_refresh(repo_root=tmp_path, reason="capture", config=config)
    enqueue_qmd_refresh(repo_root=tmp_path, reason="update", config=config)
    adapter = _FakeAdapter()
    collections = {"mnemos-project": tmp_path / "vault"}
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda _repo_root: (adapter, collections),
    )

    # when
    sut = process_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut.processed == 2
    assert sut.failed == 0
    assert len(adapter.calls) == 1
    assert adapter.calls[0] == collections
    diagnostics = get_qmd_refresh_diagnostics(repo_root=tmp_path)
    assert diagnostics["pending"] == 0
    assert diagnostics["done"] == 2


def test_failure_case_qmd_refresh_retries_then_isolates_failed_jobs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A permanently failing derived index must not poison later queue work."""
    from core.config import QmdConfig
    from core.qmd_queue import (
        WorkerStartResult,
        enqueue_qmd_refresh,
        get_qmd_refresh_diagnostics,
        process_qmd_refresh,
    )

    # given
    monkeypatch.setattr(
        "core.qmd_queue.start_qmd_index_worker",
        lambda _repo_root: WorkerStartResult(started=False),
    )
    job = enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="delete",
        config=QmdConfig(enabled=True),
    )
    adapter = _FakeAdapter(fail=True)
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda _repo_root: (adapter, {"mnemos-project": tmp_path / "vault"}),
    )

    # when
    first = process_qmd_refresh(repo_root=tmp_path, max_attempts=2)
    second = process_qmd_refresh(repo_root=tmp_path, max_attempts=2)

    # then
    assert (first.retried, first.failed) == (1, 0)
    assert (second.retried, second.failed) == (0, 1)
    diagnostics = get_qmd_refresh_diagnostics(repo_root=tmp_path)
    assert diagnostics["pending"] == 0
    assert diagnostics["failed"] == 1
    failed_payload = json.loads(
        next((job.job_path.parent.parent / "failed").glob("*.json")).read_text()
    )
    assert failed_payload["error_code"] == "nonzero_exit"
    assert "fake qmd" not in json.dumps(failed_payload)


def test_failure_case_qmd_refresh_isolates_malformed_and_generic_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """One malformed job and one unexpected adapter failure are isolated by code."""
    import core.qmd_queue as qmd_queue

    # given
    pending = tmp_path / ".agent" / "state" / "qmd-refresh" / "pending"
    pending.mkdir(parents=True)
    (pending / "malformed.json").write_text("[]", encoding="utf-8")
    (pending / "generic.json").write_text(
        json.dumps(
            {
                "job_id": "generic",
                "reason": "capture",
                "attempts": "invalid",
            }
        ),
        encoding="utf-8",
    )

    class FailingAdapter:
        def update_index(self, _collections: dict[str, Path]) -> None:
            raise RuntimeError("private adapter detail")

    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (FailingAdapter(), {"mnemos-project": tmp_path / "vault"}),
    )

    # when
    sut = qmd_queue.process_qmd_refresh(repo_root=tmp_path, max_attempts=1)

    # then
    assert sut.failed == 2
    failed_payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((pending.parent / "failed").glob("*.json"))
    ]
    assert {payload["error_code"] for payload in failed_payloads} == {
        "RuntimeError",
        "TypeError",
    }
    assert "private adapter detail" not in json.dumps(failed_payloads)


def test_failure_case_qmd_refresh_records_unexpected_worker_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A queue-level crash writes bounded failed status and still releases its lease."""
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setattr(
        qmd_queue,
        "_recover_processing_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
    )

    # when
    with pytest.raises(RuntimeError):
        qmd_queue.process_qmd_refresh(repo_root=tmp_path)
    diagnostics = qmd_queue.get_qmd_refresh_diagnostics(repo_root=tmp_path)

    # then
    assert diagnostics["worker"] == {
        "state": "failed",
        "pid": os.getpid(),
        "error_code": "RuntimeError",
    }
    assert not (qmd_queue._queue_root(tmp_path) / "worker.lock").exists()


def test_boundary_case_qmd_refresh_refuses_a_live_worker_lease(
    tmp_path: Path,
) -> None:
    """A second worker cannot claim or retry work while the owner is alive."""
    import core.qmd_queue as qmd_queue

    # given
    root = qmd_queue._queue_root(tmp_path)
    lease = qmd_queue._acquire_worker_lease(root)
    assert lease is not None

    # when
    processed = qmd_queue.process_qmd_refresh(repo_root=tmp_path)
    retried = qmd_queue.retry_failed_qmd_refresh(repo_root=tmp_path)

    # then
    assert processed.skipped_locked is True
    assert retried == 0
    qmd_queue._release_worker_lease(lease)


def test_boundary_case_qmd_refresh_drains_job_enqueued_during_active_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A job arriving behind an active batch must trigger a subsequent refresh."""
    from core.config import QmdConfig
    from core.qmd_queue import (
        WorkerStartResult,
        drain_qmd_refresh,
        enqueue_qmd_refresh,
        get_qmd_refresh_diagnostics,
    )

    # given
    monkeypatch.setattr(
        "core.qmd_queue.start_qmd_index_worker",
        lambda _repo_root: WorkerStartResult(started=False),
    )
    config = QmdConfig(enabled=True)
    enqueue_qmd_refresh(repo_root=tmp_path, reason="capture", config=config)
    adapter = _FakeAdapter()
    adapter.on_update = lambda: enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="update",
        config=config,
    )
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda _repo_root: (adapter, {"mnemos-project": tmp_path / "vault"}),
    )

    # when
    sut = drain_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut.processed == 2
    assert len(adapter.calls) == 2
    assert get_qmd_refresh_diagnostics(repo_root=tmp_path)["pending"] == 0


def test_boundary_case_fast_worker_does_not_have_idle_status_overwritten(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A child that finishes during Popen must own the final worker status."""
    import core.qmd_queue as qmd_queue

    # given
    class FastProcess:
        pid = 987654321

        def poll(self) -> int:
            return 0

    def finish_during_start(*_args: Any, **_kwargs: Any) -> FastProcess:
        qmd_queue._write_worker_status(
            qmd_queue._queue_root(tmp_path),
            state="idle",
            pid=None,
        )
        return FastProcess()

    monkeypatch.setattr(qmd_queue.subprocess, "Popen", finish_during_start)

    # when
    sut = qmd_queue.start_qmd_index_worker(tmp_path)
    diagnostics = qmd_queue.get_qmd_refresh_diagnostics(repo_root=tmp_path)

    # then
    assert sut.started is True
    assert diagnostics["worker"]["state"] == "idle"


def test_boundary_case_qmd_diagnostics_identify_stale_lock_and_status(
    tmp_path: Path,
) -> None:
    """Malformed locks and orphaned launch states remain operator-visible."""
    import core.qmd_queue as qmd_queue

    # given
    root = qmd_queue._queue_root(tmp_path)
    lock_path = root / "worker.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "owner.json").write_text("invalid", encoding="utf-8")

    # when
    malformed_lock = qmd_queue.get_qmd_refresh_diagnostics(repo_root=tmp_path)
    for path in lock_path.iterdir():
        path.unlink()
    lock_path.rmdir()
    qmd_queue._write_worker_status(
        root,
        state="starting",
        pid=None,
        error_code="worker_start_failed",
    )
    orphaned_status = qmd_queue.get_qmd_refresh_diagnostics(repo_root=tmp_path)

    # then
    assert malformed_lock["worker"]["state"] == "stale"
    assert orphaned_status["worker"] == {
        "state": "stale",
        "pid": None,
        "error_code": "worker_start_failed",
    }


def test_boundary_case_finishing_worker_does_not_clobber_new_worker_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Final status must be written before releasing the single-flight lease."""
    from core.config import QmdConfig
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setattr(
        qmd_queue,
        "start_qmd_index_worker",
        lambda _repo_root: qmd_queue.WorkerStartResult(started=False),
    )
    qmd_queue.enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="capture",
        config=QmdConfig(enabled=True),
    )
    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (
            _FakeAdapter(),
            {"mnemos-project": tmp_path / "vault"},
        ),
    )

    def simulate_new_worker(_lease: Any) -> None:
        qmd_queue._write_worker_status(
            qmd_queue._queue_root(tmp_path),
            state="running",
            pid=os.getpid(),
        )

    monkeypatch.setattr(qmd_queue, "_release_worker_lease", simulate_new_worker)

    # when
    qmd_queue.process_qmd_refresh(repo_root=tmp_path)
    status = json.loads(
        (qmd_queue._queue_root(tmp_path) / "worker-status.json").read_text()
    )

    # then
    assert status["state"] == "running"


def test_success_case_qmd_index_worker_status_cli_is_machine_readable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Operators can inspect QMD queue health without consuming pending jobs."""
    from core.cli import cli
    from core.config import QmdConfig
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        qmd_queue,
        "start_qmd_index_worker",
        lambda _repo_root: qmd_queue.WorkerStartResult(started=False),
    )
    queued = qmd_queue.enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="capture",
        config=QmdConfig(enabled=True),
    )
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-index-worker", "--status", "--json"])

    # then
    assert result.exit_code == 0
    assert json.loads(result.output)["queue"]["pending"] == 1
    assert queued.job_path is not None and queued.job_path.exists()


def test_success_case_qmd_index_worker_text_modes_report_bounded_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Human-readable status, retry, and drain paths remain script-safe."""
    from core.cli import cli

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    sut = CliRunner()

    # when
    status = sut.invoke(cli, ["qmd-index-worker", "--status"])
    retry = sut.invoke(cli, ["qmd-index-worker", "--retry-failed"])
    drain = sut.invoke(cli, ["qmd-index-worker"])

    # then
    assert status.exit_code == 0
    assert status.output == "pending=0 processing=0 failed=0 worker=idle\n"
    assert retry.exit_code == 0
    assert retry.output == "requeued=0 worker_started=False\n"
    assert drain.exit_code == 0
    assert drain.output == "processed=0 failed=0 retried=0 recovered=0\n"


def test_failure_case_qmd_index_worker_rejects_conflicting_lifecycle_options(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Read-only status and mutating recovery cannot be requested together."""
    from core.cli import cli

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    sut = CliRunner()

    # when
    result = sut.invoke(
        cli,
        ["qmd-index-worker", "--status", "--retry-failed"],
    )

    # then
    assert result.exit_code == 2
    assert "--status and --retry-failed are mutually exclusive" in result.output


def test_success_case_detached_qmd_worker_consumes_durable_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A persisted refresh must be consumed without a resident daemon."""
    from core.config import QmdConfig
    from core.qmd_queue import enqueue_qmd_refresh, get_qmd_refresh_diagnostics

    # given
    _write_minimal_policy(tmp_path)
    executable = _write_fake_qmd_executable(tmp_path / "fake-qmd")
    calls_log = tmp_path / "qmd-calls.jsonl"
    monkeypatch.setenv("FAKE_QMD_CALLS_LOG", str(calls_log))
    (tmp_path / "mnemos.yml").write_text(
        yaml.safe_dump(
            {
                "retrieval": {
                    "qmd": {
                        "enabled": True,
                        "executable": str(executable),
                        "update_timeout_seconds": 2,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    started = time.perf_counter()

    # when
    sut = enqueue_qmd_refresh(
        repo_root=tmp_path,
        reason="capture",
        config=QmdConfig(enabled=True, executable=str(executable)),
    )
    enqueue_duration = time.perf_counter() - started
    deadline = time.monotonic() + 10
    diagnostics = get_qmd_refresh_diagnostics(repo_root=tmp_path)
    while diagnostics["done"] < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
        diagnostics = get_qmd_refresh_diagnostics(repo_root=tmp_path)

    # then
    assert sut.worker_started is True
    assert enqueue_duration < 1.0
    assert diagnostics["pending"] == 0
    assert diagnostics["done"] == 1
    calls = [json.loads(line)["argv"] for line in calls_log.read_text().splitlines()]
    assert calls == [["--index", "mnemos", "update"]]


def test_success_case_failed_qmd_refresh_jobs_can_be_requeued(
    tmp_path: Path,
) -> None:
    """Operator recovery resets bounded retry metadata and restores pending work."""
    from core.qmd_queue import get_qmd_refresh_diagnostics, retry_failed_qmd_refresh

    # given
    failed_dir = tmp_path / ".agent" / "state" / "qmd-refresh" / "failed"
    failed_dir.mkdir(parents=True)
    failed_path = failed_dir / "retry-me.json"
    failed_path.write_text(
        json.dumps(
            {
                "job_id": "retry-me",
                "reason": "capture",
                "queued_at": "2026-08-12T00:00:00+00:00",
                "attempts": 3,
                "failed_at": "2026-08-12T00:01:00+00:00",
                "last_failed_at": "2026-08-12T00:01:00+00:00",
                "error_code": "nonzero_exit",
            }
        ),
        encoding="utf-8",
    )

    # when
    sut = retry_failed_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut == 1
    diagnostics = get_qmd_refresh_diagnostics(repo_root=tmp_path)
    assert diagnostics["failed"] == 0
    assert diagnostics["pending"] == 1
    payload = json.loads(
        (failed_dir.parent / "pending" / "retry-me.json").read_text()
    )
    assert payload["attempts"] == 0
    assert "failed_at" not in payload
    assert "last_failed_at" not in payload
    assert "error_code" not in payload


def test_boundary_case_qmd_retry_scrubs_legacy_private_reason(
    tmp_path: Path,
) -> None:
    """Recovery of pre-hardening jobs cannot repersist caller-provided text."""
    from core.qmd_queue import retry_failed_qmd_refresh

    # given
    private_reason = "private memory content from an old queue version"
    failed_dir = tmp_path / ".agent" / "state" / "qmd-refresh" / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "legacy.json").write_text(
        json.dumps(
            {
                "job_id": "legacy",
                "reason": private_reason,
                "attempts": 3,
            }
        ),
        encoding="utf-8",
    )

    # when
    sut = retry_failed_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut == 1
    payload = json.loads(
        (failed_dir.parent / "pending" / "legacy.json").read_text(encoding="utf-8")
    )
    assert payload["reason"] == "other"
    assert private_reason not in json.dumps(payload)


def test_boundary_case_qmd_retry_skips_malformed_terminal_jobs(
    tmp_path: Path,
) -> None:
    """Operator retry must preserve invalid evidence for inspection."""
    from core.qmd_queue import retry_failed_qmd_refresh

    # given
    failed_dir = tmp_path / ".agent" / "state" / "qmd-refresh" / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "invalid-json.json").write_text("not-json", encoding="utf-8")
    (failed_dir / "missing-id.json").write_text(
        json.dumps({"reason": "capture"}),
        encoding="utf-8",
    )
    (failed_dir / "missing-reason.json").write_text(
        json.dumps({"job_id": "missing-reason"}),
        encoding="utf-8",
    )

    # when
    sut = retry_failed_qmd_refresh(repo_root=tmp_path)

    # then
    assert sut == 0
    assert len(list(failed_dir.glob("*.json"))) == 3
    assert not any((failed_dir.parent / "pending").glob("*.json"))


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [("success", 0), ("failed_jobs", 1), ("exception", 1)],
)
def test_boundary_case_qmd_worker_entrypoint_normalizes_terminal_outcomes(
    tmp_path: Path,
    monkeypatch,
    outcome: str,
    expected: int,
) -> None:
    """Detached process exit codes and crash status are deterministic."""
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    if outcome == "success":
        monkeypatch.setattr(
            qmd_queue,
            "drain_qmd_refresh",
            lambda **_kwargs: qmd_queue.QmdRefreshResult(processed=1, failed=0),
        )
    elif outcome == "failed_jobs":
        monkeypatch.setattr(
            qmd_queue,
            "drain_qmd_refresh",
            lambda **_kwargs: qmd_queue.QmdRefreshResult(processed=0, failed=1),
        )
    else:
        monkeypatch.setattr(
            qmd_queue,
            "drain_qmd_refresh",
            lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private")),
        )

    # when
    sut = qmd_queue.worker_entrypoint()

    # then
    assert sut == expected
    if outcome == "exception":
        diagnostics = qmd_queue.get_qmd_refresh_diagnostics(repo_root=tmp_path)
        assert diagnostics["worker"]["error_code"] == "RuntimeError"


def test_success_case_qmd_retry_cli_requeues_and_signals_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Recovery CLI returns promptly after durable requeue and worker signal."""
    from core.cli import cli
    import core.qmd_queue as qmd_queue

    # given
    failed_dir = tmp_path / ".agent" / "state" / "qmd-refresh" / "failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "retry.json").write_text(
        json.dumps(
            {
                "job_id": "retry",
                "reason": "update",
                "queued_at": "2026-08-12T00:00:00+00:00",
                "attempts": 3,
                "error_code": "timeout",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        qmd_queue,
        "start_qmd_index_worker",
        lambda _repo_root: qmd_queue.WorkerStartResult(started=True, pid=4321),
    )
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-index-worker", "--retry-failed", "--json"])

    # then
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["requeued"] == 1
    assert payload["worker"] == {"started": True, "pid": 4321, "error_code": None}
    assert payload["queue"]["pending"] == 1


def test_success_case_qmd_prepare_cli_writes_config_without_running_qmd(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Preparation creates only repo-local derived configuration."""
    from core.cli import cli
    import core.qmd_queue as qmd_queue

    # given
    calls: list[dict[str, Path]] = []

    class Adapter:
        def prepare_index_config(self, collections: dict[str, Path]) -> Path:
            calls.append(dict(collections))
            path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
            path.parent.mkdir(parents=True)
            path.write_text("collections: {}\n", encoding="utf-8")
            return path

    collections = {"mnemos-project": tmp_path / "wiki"}
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (Adapter(), collections),
    )
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-prepare", "--json"])

    # then
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["prepared"] is True
    assert payload["collection_count"] == 1
    assert Path(payload["config_path"]).is_file()
    assert calls == [collections]


def test_success_case_qmd_prepare_cli_reports_human_readable_local_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Text setup output identifies the local derived config without invoking QMD."""
    from core.cli import cli
    import core.qmd_queue as qmd_queue

    # given
    class Adapter:
        def prepare_index_config(self, _collections: dict[str, Path]) -> Path:
            return tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (Adapter(), {"mnemos-project": tmp_path / "wiki"}),
    )
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-prepare"])

    # then
    assert result.exit_code == 0
    assert result.output.startswith("prepared=True collections=1 config=")
    assert result.output.rstrip().endswith("config/mnemos.yml")


def test_failure_case_qmd_prepare_cli_reports_setup_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Preparation errors must become ordinary bounded CLI failures."""
    from core.cli import cli
    import core.qmd_queue as qmd_queue

    # given
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(
        qmd_queue,
        "_build_qmd_adapter",
        lambda _repo_root: (_ for _ in ()).throw(OSError("config unavailable")),
    )
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-prepare"])

    # then
    assert result.exit_code == 1
    assert result.output == "Error: config unavailable\n"


def test_success_case_qmd_prepare_cli_builds_real_repo_local_collections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The setup command integrates with the canonical default store without QMD."""
    from core.cli import cli

    # given
    _write_minimal_policy(tmp_path)
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
    sut = CliRunner()

    # when
    result = sut.invoke(cli, ["qmd-prepare", "--json"])

    # then
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["collection_count"] == 4
    config = yaml.safe_load(Path(payload["config_path"]).read_text(encoding="utf-8"))
    assert set(config["collections"]) == {
        "mnemos-runs",
        "mnemos-sessions",
        "mnemos-transient",
        "mnemos-wiki",
    }


def test_success_case_qmd_collection_roots_use_single_obsidian_vault(
    tmp_path: Path,
) -> None:
    """Obsidian-backed memory must not index unrelated repository Markdown."""
    from core.qmd_queue import qmd_collection_roots

    # given
    vault = tmp_path / "vault"
    store = type("ObsidianStore", (), {"_vault": vault})()

    # when
    sut = qmd_collection_roots(repo_root=tmp_path, store=store)

    # then
    assert sut == {"mnemos-vault": vault.resolve()}
