"""Durable asynchronous capture queue."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.gateway import MemoryGateway


@dataclass(frozen=True)
class QueuedCapture:
    item_id: str
    status: str
    path: Path
    worker_started: bool = False
    worker_pid: int | None = None
    worker_error_code: str | None = None


@dataclass(frozen=True)
class WorkerStartResult:
    started: bool
    pid: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class CaptureQueueResult:
    processed: int
    failed: int
    retried: int = 0
    recovered: int = 0
    skipped_locked: bool = False


@dataclass(frozen=True)
class _WorkerLease:
    path: Path
    token: str
    reclaimed_abandoned_lock: bool = False


_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RECOVERY_AFTER_SECONDS = 300.0
_LOCK_INITIALIZATION_GRACE_SECONDS = 5.0
_DETACHED_WORKERS: dict[int, subprocess.Popen] = {}
_QUEUE_STATES = ("pending", "processing", "done", "failed")


def _queue_root(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".agent" / "state" / "capture-queue"


def _reap_detached_workers() -> None:
    for pid, process in list(_DETACHED_WORKERS.items()):
        if process.poll() is not None:
            _DETACHED_WORKERS.pop(pid, None)


def _capture_worker_command() -> list[str]:
    adjacent_entrypoint = Path(sys.executable).with_name("mnemos")
    if adjacent_entrypoint.is_file():
        return [str(adjacent_entrypoint), "capture-worker"]

    installed_entrypoint = shutil.which("mnemos")
    if installed_entrypoint:
        return [installed_entrypoint, "capture-worker"]

    return [
        sys.executable,
        "-c",
        "from core.capture_queue import worker_entrypoint; raise SystemExit(worker_entrypoint())",
    ]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _worker_status_path(root: Path) -> Path:
    return root / "worker-status.json"


def _write_worker_status(
    root: Path,
    *,
    state: str,
    pid: int | None,
    error_code: str | None = None,
    token: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "state": state,
        "pid": pid,
        "error_code": error_code,
        "updated_at": _now_iso(),
    }
    if token is not None:
        payload["token"] = token
    _write_json_atomic(_worker_status_path(root), payload)


def _read_worker_status(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_worker_status_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _worker_lock_is_abandoned(lock_path: Path) -> bool:
    owner_path = lock_path / "owner.json"
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = int(owner["pid"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        try:
            age = max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            return False
        return age >= _LOCK_INITIALIZATION_GRACE_SECONDS
    return not _pid_is_running(pid)


def _acquire_worker_lease(root: Path) -> _WorkerLease | None:
    lock_path = root / "worker.lock"
    root.mkdir(parents=True, exist_ok=True)
    reclaimed_abandoned_lock = False

    for _attempt in range(3):
        try:
            lock_path.mkdir()
        except FileExistsError:
            if not _worker_lock_is_abandoned(lock_path):
                return None
            abandoned_path = root / f"worker.lock.abandoned-{uuid.uuid4().hex}"
            try:
                lock_path.replace(abandoned_path)
            except FileNotFoundError:
                continue
            shutil.rmtree(abandoned_path, ignore_errors=True)
            reclaimed_abandoned_lock = True
            continue

        token = uuid.uuid4().hex
        try:
            _write_json_atomic(
                lock_path / "owner.json",
                {"pid": os.getpid(), "token": token, "started_at": _now_iso()},
            )
        except Exception:
            shutil.rmtree(lock_path, ignore_errors=True)
            raise
        return _WorkerLease(
            path=lock_path,
            token=token,
            reclaimed_abandoned_lock=reclaimed_abandoned_lock,
        )

    return None


def _release_worker_lease(lease: _WorkerLease) -> None:
    try:
        owner = json.loads((lease.path / "owner.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if owner.get("token") != lease.token:
        return

    released_path = lease.path.with_name(f"worker.lock.released-{lease.token}")
    try:
        lease.path.replace(released_path)
    except FileNotFoundError:
        return
    shutil.rmtree(released_path, ignore_errors=True)


def _recover_abandoned_jobs(
    *,
    processing_dir: Path,
    pending_dir: Path,
    recovery_after_seconds: float,
) -> int:
    recovered = 0
    now = time.time()
    for processing_path in sorted(processing_dir.glob("*.json")):
        try:
            age = max(0.0, now - processing_path.stat().st_mtime)
        except FileNotFoundError:
            continue
        if age < recovery_after_seconds:
            continue
        try:
            processing_path.replace(pending_dir / processing_path.name)
        except FileNotFoundError:
            continue
        recovered += 1
    return recovered


def _isolate_malformed_job(
    processing_path: Path,
    failed_dir: Path,
    exc: Exception,
    raw_payload: Any = None,
) -> None:
    item_id = raw_payload.get("item_id") if isinstance(raw_payload, dict) else None
    job_id = raw_payload.get("job_id") if isinstance(raw_payload, dict) else None
    receipt = {
        "job_id": job_id if isinstance(job_id, str) and job_id else processing_path.stem,
        "item_id": item_id if isinstance(item_id, str) and item_id else processing_path.stem,
        "status": "failed",
        "failed_at": _now_iso(),
        "error_type": exc.__class__.__name__,
    }
    _write_json_atomic(failed_dir / processing_path.name, receipt)
    processing_path.unlink(missing_ok=True)


def _terminal_receipt(
    payload: dict[str, Any],
    *,
    status: str,
    error_type: str | None = None,
) -> dict[str, Any]:
    try:
        attempts = int(payload.get("attempts", 0))
    except (TypeError, ValueError):
        attempts = 0
    receipt: dict[str, Any] = {
        "job_id": str(payload.get("job_id") or "unknown"),
        "item_id": str(payload.get("item_id") or "unknown"),
        "status": status,
        "attempts": attempts,
        "completed_at": _now_iso(),
    }
    completed_phases = payload.get("completed_phases")
    if isinstance(completed_phases, list):
        receipt["completed_phases"] = [
            phase for phase in completed_phases if isinstance(phase, str)
        ]
    if error_type is not None:
        receipt["error_type"] = error_type
    sync_failure = payload.get("sync_failure")
    if status == "captured" and isinstance(sync_failure, dict):
        receipt["status"] = "sync_pending"
        receipt.update(sync_failure)
    return receipt


def _remember_sync_failure(gateway: Any, payload: dict[str, Any]) -> None:
    failure = getattr(getattr(gateway, "_store", None), "last_sync_failure", None)
    if failure is None:
        return
    payload["sync_failure"] = {
        "capture_status": "committed",
        "sync_status": "failed",
        "retryable": bool(getattr(failure, "retryable", True)),
        "commit": getattr(failure, "commit", None),
        "error_code": getattr(failure, "error_code", "git_sync_failed"),
        "remote": getattr(failure, "remote", None),
        "branch": getattr(failure, "branch", None),
        "recovery_command": getattr(
            failure,
            "recovery_command",
            "mnemos sync pull && mnemos sync push",
        ),
    }


def _move_terminal_receipt(
    *,
    processing_path: Path,
    target_path: Path,
    receipt: dict[str, Any],
) -> None:
    _write_json_atomic(processing_path, receipt)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    processing_path.replace(target_path)


def _purge_tombstone_path(root: Path, item_id: str) -> Path:
    item_key = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return root / "tombstones" / f"{item_key}.json"


def _job_was_purged(root: Path, payload: dict[str, Any]) -> bool:
    item_id = payload.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return False
    try:
        tombstone = json.loads(
            _purge_tombstone_path(root, item_id).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(tombstone, dict):
        return False
    deleted_at = tombstone.get("deleted_at")
    if not isinstance(deleted_at, str) or not deleted_at:
        return False
    queued_at = payload.get("queued_at")
    return not isinstance(queued_at, str) or not queued_at or queued_at <= deleted_at


def _delete_purged_capture(gateway: Any, item_id: str) -> None:
    delete = getattr(gateway, "delete", None)
    if not callable(delete):
        return
    try:
        delete(item_id)
    except FileNotFoundError:
        pass


def _handle_capture_failure(
    *,
    processing_path: Path,
    pending_dir: Path,
    failed_dir: Path,
    payload: dict[str, Any],
    exc: Exception,
    max_attempts: int,
) -> str:
    try:
        attempts = int(payload.get("attempts", 0)) + 1
    except (TypeError, ValueError):
        attempts = 1
    payload["attempts"] = attempts
    payload["last_failed_at"] = _now_iso()
    payload["error_type"] = exc.__class__.__name__
    _write_json_atomic(processing_path, payload)

    if attempts < max_attempts:
        processing_path.replace(pending_dir / processing_path.name)
        return "retried"

    receipt = _terminal_receipt(
        payload,
        status="failed",
        error_type=exc.__class__.__name__,
    )
    _move_terminal_receipt(
        processing_path=processing_path,
        target_path=failed_dir / processing_path.name,
        receipt=receipt,
    )
    return "failed"


def purge_capture_jobs(*, repo_root: str | Path, item_id: str) -> int:
    """Remove queue payloads and receipts associated with a hard-deleted item."""
    removed = 0
    root = _queue_root(repo_root)
    _write_json_atomic(
        _purge_tombstone_path(root, item_id),
        {"deleted_at": _now_iso()},
    )
    for state in _QUEUE_STATES:
        for path in (root / state).glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("item_id") != item_id:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed += 1
    return removed


def get_capture_queue_diagnostics(*, repo_root: str | Path) -> dict[str, Any]:
    """Return content-free queue depth, age, and worker-health diagnostics."""
    _reap_detached_workers()
    root = _queue_root(repo_root)
    pending_paths = list((root / "pending").glob("*.json"))
    processing_paths = list((root / "processing").glob("*.json"))
    failed_paths = list((root / "failed").glob("*.json"))
    done_paths = list((root / "done").glob("*.json"))
    sync_pending = 0
    for path in done_paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "sync_pending":
            sync_pending += 1

    oldest_age: float | None = None
    if pending_paths:
        mtimes: list[float] = []
        for path in pending_paths:
            try:
                mtimes.append(path.stat().st_mtime)
            except FileNotFoundError:
                continue
        if mtimes:
            oldest_age = round(max(0.0, time.time() - min(mtimes)), 3)

    worker = {"state": "idle", "pid": None, "error_code": None}
    lock_path = root / "worker.lock"
    if lock_path.exists():
        try:
            owner = json.loads((lock_path / "owner.json").read_text(encoding="utf-8"))
            pid = int(owner["pid"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            worker["state"] = "stale"
        else:
            worker["state"] = "running" if _pid_is_running(pid) else "stale"
            worker["pid"] = pid
    else:
        status = _read_worker_status(root)
        state = str(status.get("state") or "idle")
        pid_value = status.get("pid")
        pid = pid_value if isinstance(pid_value, int) else None
        if state in {"launching", "starting", "running"}:
            state = state if pid is not None and _pid_is_running(pid) else "stale"
        if state in {"idle", "launch_failed", "failed", "starting", "stale"}:
            worker["state"] = state
        worker["pid"] = pid
        error_code = status.get("error_code")
        worker["error_code"] = str(error_code) if error_code else None

    return {
        "pending": len(pending_paths),
        "processing": len(processing_paths),
        "failed": len(failed_paths),
        "done": len(done_paths),
        "sync_pending": sync_pending,
        "oldest_pending_age_seconds": oldest_age,
        "worker": worker,
    }


def start_capture_worker(repo_root: str | Path) -> WorkerStartResult:
    """Start a detached one-shot worker using the current Python environment."""
    _reap_detached_workers()
    root = _queue_root(repo_root)
    launch_token = uuid.uuid4().hex
    _write_worker_status(
        root,
        state="launching",
        pid=None,
        token=launch_token,
    )
    env = os.environ.copy()
    env["MNEMOS_REPO_ROOT"] = str(Path(repo_root).expanduser().resolve())
    command = _capture_worker_command()

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError):
        _write_worker_status(
            root,
            state="launch_failed",
            pid=None,
            error_code="worker_start_failed",
        )
        return WorkerStartResult(started=False, error_code="worker_start_failed")

    current_status = _read_worker_status(root)
    if current_status.get("token") == launch_token:
        _write_worker_status(root, state="starting", pid=process.pid)
    _DETACHED_WORKERS[process.pid] = process
    return WorkerStartResult(started=True, pid=process.pid)


def worker_entrypoint() -> int:
    """Process queued captures in a detached worker process."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    root = _queue_root(repo_root)
    try:
        result = drain_pending_captures(repo_root=repo_root)
        return 1 if result.failed else 0
    except Exception as exc:
        _write_worker_status(
            root,
            state="failed",
            pid=os.getpid(),
            error_code=exc.__class__.__name__,
        )
        return 1


def enqueue_capture(
    *,
    repo_root: str | Path,
    content: str,
    layer: str | None,
    tags: list[str] | None = None,
    quality_score: float = 0.8,
    run_id: str | None = None,
    session_id: str | None = None,
    item_id: str | None = None,
    no_classify: bool = False,
) -> QueuedCapture:
    """Persist a capture job without materializing it into the memory store."""
    resolved_id = item_id or str(uuid.uuid4())
    job_id = uuid.uuid4().hex
    pending_dir = _queue_root(repo_root) / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    job_path = pending_dir / f"{job_id}.json"
    payload: dict[str, Any] = {
        "job_id": job_id,
        "item_id": resolved_id,
        "content": content,
        "layer": layer,
        "tags": tags or [],
        "quality_score": quality_score,
        "run_id": run_id,
        "session_id": session_id,
        "no_classify": no_classify,
        "queued_at": _now_iso(),
        "attempts": 0,
    }

    _write_json_atomic(job_path, payload)

    try:
        worker = start_capture_worker(repo_root)
    except Exception:
        worker = WorkerStartResult(started=False, error_code="worker_start_failed")

    return QueuedCapture(
        item_id=resolved_id,
        status="queued",
        path=job_path,
        worker_started=worker.started,
        worker_pid=worker.pid,
        worker_error_code=worker.error_code,
    )


def process_pending_captures(
    *,
    repo_root: str | Path,
    limit: int | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    recovery_after_seconds: float = _DEFAULT_RECOVERY_AFTER_SECONDS,
) -> CaptureQueueResult:
    """Materialize pending capture jobs through the normal MemoryGateway path."""
    root = _queue_root(repo_root)
    pending_dir = root / "pending"
    processing_dir = root / "processing"
    done_dir = root / "done"
    failed_dir = root / "failed"
    pending_dir.mkdir(parents=True, exist_ok=True)
    processing_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    lease = _acquire_worker_lease(root)
    if lease is None:
        return CaptureQueueResult(processed=0, failed=0, skipped_locked=True)
    _write_worker_status(root, state="running", pid=os.getpid())

    attempted = 0
    processed = 0
    failed = 0
    retried = 0
    recovered = 0
    gateway: MemoryGateway | None = None
    max_attempts = max(1, int(max_attempts))
    recovery_after_seconds = max(0.0, float(recovery_after_seconds))

    try:
        recovered = _recover_abandoned_jobs(
            processing_dir=processing_dir,
            pending_dir=pending_dir,
            recovery_after_seconds=(
                0.0 if lease.reclaimed_abandoned_lock else recovery_after_seconds
            ),
        )

        for job_path in sorted(pending_dir.glob("*.json")):
            if limit is not None and attempted >= limit:
                break
            processing_path = processing_dir / job_path.name
            try:
                job_path.replace(processing_path)
            except FileNotFoundError:
                continue

            attempted += 1
            payload: Any = None
            try:
                payload = json.loads(processing_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise TypeError("capture job must be an object")
                content = payload["content"]
                item_id = payload["item_id"]
                if not isinstance(content, str) or not content:
                    raise ValueError("capture job content must be non-empty text")
                if not isinstance(item_id, str) or not item_id:
                    raise ValueError("capture job item_id must be non-empty text")
            except Exception as exc:
                _isolate_malformed_job(
                    processing_path,
                    failed_dir,
                    exc,
                    raw_payload=payload,
                )
                if isinstance(payload, dict) and _job_was_purged(root, payload):
                    (failed_dir / processing_path.name).unlink(missing_ok=True)
                failed += 1
                continue

            try:
                resume_store = bool(payload.get("materialization_started"))
                if gateway is None:
                    gateway = MemoryGateway(repo_root=str(repo_root))
                capture_kwargs = {
                    "content": content,
                    "layer": payload.get("layer"),
                    "tags": list(payload.get("tags") or []),
                    "quality_score": float(payload.get("quality_score", 0.8)),
                    "run_id": payload.get("run_id"),
                    "session_id": payload.get("session_id"),
                    "item_id": item_id,
                    "no_classify": bool(payload.get("no_classify", False)),
                }
                capture_with_progress = getattr(gateway, "capture_with_progress", None)
                if callable(capture_with_progress):
                    raw_completed_phases = payload.get("completed_phases") or []
                    completed_phases = {
                        str(phase)
                        for phase in raw_completed_phases
                        if isinstance(phase, str)
                    }

                    def record_phase(phase: str) -> None:
                        phases = payload.setdefault("completed_phases", [])
                        if phase not in phases:
                            phases.append(phase)
                            if phase == "store_write":
                                _remember_sync_failure(gateway, payload)
                            _write_json_atomic(processing_path, payload)

                    def record_store_attempt() -> None:
                        if payload.get("materialization_started"):
                            return
                        payload["materialization_started"] = True
                        _write_json_atomic(processing_path, payload)

                    capture_with_progress(
                        completed_phases=completed_phases,
                        on_phase=record_phase,
                        on_store_attempt=record_store_attempt,
                        resume_store=resume_store,
                        **capture_kwargs,
                    )
                else:
                    gateway.capture(**capture_kwargs)
                _remember_sync_failure(gateway, payload)
            except Exception as exc:
                if _job_was_purged(root, payload):
                    if gateway is not None:
                        _delete_purged_capture(gateway, item_id)
                    processing_path.unlink(missing_ok=True)
                    continue
                outcome = _handle_capture_failure(
                    processing_path=processing_path,
                    pending_dir=pending_dir,
                    failed_dir=failed_dir,
                    payload=payload,
                    exc=exc,
                    max_attempts=max_attempts,
                )
                if _job_was_purged(root, payload):
                    (pending_dir / processing_path.name).unlink(missing_ok=True)
                    (failed_dir / processing_path.name).unlink(missing_ok=True)
                    if gateway is not None:
                        _delete_purged_capture(gateway, item_id)
                    continue
                if outcome == "retried":
                    retried += 1
                else:
                    failed += 1
                continue

            if _job_was_purged(root, payload):
                _delete_purged_capture(gateway, item_id)
                processing_path.unlink(missing_ok=True)
                continue

            receipt = _terminal_receipt(payload, status="captured")
            done_path = done_dir / processing_path.name
            _move_terminal_receipt(
                processing_path=processing_path,
                target_path=done_path,
                receipt=receipt,
            )
            if _job_was_purged(root, payload):
                done_path.unlink(missing_ok=True)
                _delete_purged_capture(gateway, item_id)
                continue
            processed += 1
    except Exception as exc:
        _write_worker_status(
            root,
            state="failed",
            pid=os.getpid(),
            error_code=exc.__class__.__name__,
        )
        raise
    else:
        _write_worker_status(
            root,
            state="idle",
            pid=None,
            error_code="jobs_failed" if failed else None,
        )
    finally:
        _release_worker_lease(lease)

    return CaptureQueueResult(
        processed=processed,
        failed=failed,
        retried=retried,
        recovered=recovered,
    )


def drain_pending_captures(
    *,
    repo_root: str | Path,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    recovery_after_seconds: float = _DEFAULT_RECOVERY_AFTER_SECONDS,
) -> CaptureQueueResult:
    """Drain retries in one worker lifecycle until no retryable job remains."""
    processed = 0
    failed = 0
    retried = 0
    recovered = 0

    while True:
        result = process_pending_captures(
            repo_root=repo_root,
            max_attempts=max_attempts,
            recovery_after_seconds=recovery_after_seconds,
        )
        processed += result.processed
        failed += result.failed
        retried += result.retried
        recovered += result.recovered
        if result.skipped_locked:
            return CaptureQueueResult(
                processed=processed,
                failed=failed,
                retried=retried,
                recovered=recovered,
                skipped_locked=True,
            )
        pending_dir = _queue_root(repo_root) / "pending"
        if result.retried == 0 and not any(pending_dir.glob("*.json")):
            return CaptureQueueResult(
                processed=processed,
                failed=failed,
                retried=retried,
                recovered=recovered,
            )
        time.sleep(0.1)
