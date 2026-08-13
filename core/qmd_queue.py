"""Durable, coalescing refresh queue for the optional QMD derived index."""
from __future__ import annotations

import datetime
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

from core.config import QmdConfig, get_qmd_config
from core.qmd import QmdAdapter, QmdCommandError


@dataclass(frozen=True)
class WorkerStartResult:
    started: bool
    pid: int | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class QmdRefreshEnqueueResult:
    enabled: bool
    queued: bool
    job_path: Path | None = None
    worker_started: bool = False
    worker_pid: int | None = None
    worker_error_code: str | None = None


@dataclass(frozen=True)
class QmdRefreshResult:
    processed: int
    failed: int
    retried: int = 0
    recovered: int = 0
    skipped_locked: bool = False


@dataclass(frozen=True)
class _WorkerLease:
    path: Path
    token: str


_LOCK_INITIALIZATION_GRACE_SECONDS = 5.0
_DEFAULT_MAX_ATTEMPTS = 3
_DETACHED_WORKERS: dict[int, subprocess.Popen] = {}
_REFRESH_REASONS = {
    "archive",
    "auto_classify",
    "capture",
    "classify",
    "delete",
    "demote",
    "forget",
    "promote",
    "update",
}


def _bounded_reason(reason: Any) -> str:
    return reason if isinstance(reason, str) and reason in _REFRESH_REASONS else "other"


def _queue_root(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".agent" / "state" / "qmd-refresh"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("qmd refresh job must be an object")
    return payload


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


def _lock_is_abandoned(lock_path: Path) -> bool:
    try:
        owner = _read_json(lock_path / "owner.json")
        return not _pid_is_running(int(owner["pid"]))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        try:
            age = max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            return False
        return age >= _LOCK_INITIALIZATION_GRACE_SECONDS


def _acquire_worker_lease(root: Path) -> _WorkerLease | None:
    lock_path = root / "worker.lock"
    root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        try:
            lock_path.mkdir()
        except FileExistsError:
            if not _lock_is_abandoned(lock_path):
                return None
            abandoned = root / f"worker.lock.abandoned-{uuid.uuid4().hex}"
            try:
                lock_path.replace(abandoned)
            except FileNotFoundError:
                continue
            shutil.rmtree(abandoned, ignore_errors=True)
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
        return _WorkerLease(path=lock_path, token=token)
    return None


def _release_worker_lease(lease: _WorkerLease) -> None:
    try:
        owner = _read_json(lease.path / "owner.json")
    except (OSError, TypeError, json.JSONDecodeError):
        return
    if owner.get("token") != lease.token:
        return

    released = lease.path.with_name(f"worker.lock.released-{lease.token}")
    try:
        lease.path.replace(released)
    except FileNotFoundError:
        return
    shutil.rmtree(released, ignore_errors=True)


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


def _reap_detached_workers() -> None:
    for pid, process in list(_DETACHED_WORKERS.items()):
        if process.poll() is not None:
            _DETACHED_WORKERS.pop(pid, None)


def _qmd_worker_command() -> list[str]:
    adjacent_entrypoint = Path(sys.executable).with_name("mnemos")
    if adjacent_entrypoint.is_file():
        return [str(adjacent_entrypoint), "qmd-index-worker"]

    installed_entrypoint = shutil.which("mnemos")
    if installed_entrypoint:
        return [installed_entrypoint, "qmd-index-worker"]

    return [
        sys.executable,
        "-c",
        "from core.qmd_queue import worker_entrypoint; raise SystemExit(worker_entrypoint())",
    ]


def start_qmd_index_worker(repo_root: str | Path) -> WorkerStartResult:
    """Start a detached one-shot QMD refresh worker."""
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
    try:
        process = subprocess.Popen(
            _qmd_worker_command(),
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

    try:
        current_status = _read_json(_worker_status_path(root))
    except (OSError, TypeError, json.JSONDecodeError):
        current_status = {}
    if current_status.get("token") == launch_token:
        _write_worker_status(root, state="starting", pid=process.pid)
    _DETACHED_WORKERS[process.pid] = process
    return WorkerStartResult(started=True, pid=process.pid)


def enqueue_qmd_refresh(
    *,
    repo_root: str | Path,
    reason: str,
    config: QmdConfig | None = None,
) -> QmdRefreshEnqueueResult:
    """Persist a content-free refresh job before launching its worker."""
    resolved_config = config or get_qmd_config(str(repo_root))
    if not resolved_config.enabled:
        return QmdRefreshEnqueueResult(enabled=False, queued=False)

    job_id = uuid.uuid4().hex
    job_path = _queue_root(repo_root) / "pending" / f"{job_id}.json"
    _write_json_atomic(
        job_path,
        {
            "job_id": job_id,
            "reason": _bounded_reason(reason),
            "queued_at": _now_iso(),
            "attempts": 0,
        },
    )
    try:
        worker = start_qmd_index_worker(repo_root)
    except Exception:
        worker = WorkerStartResult(
            started=False,
            error_code="worker_start_failed",
        )
    return QmdRefreshEnqueueResult(
        enabled=True,
        queued=True,
        job_path=job_path,
        worker_started=worker.started,
        worker_pid=worker.pid,
        worker_error_code=worker.error_code,
    )


def qmd_collection_roots(
    *,
    repo_root: str | Path,
    store: Any,
) -> dict[str, Path]:
    """Return canonical memory roots without including QMD's derived state."""
    vault = getattr(store, "_vault", None)
    if vault is not None:
        return {"mnemos-vault": Path(vault).expanduser().resolve()}

    root = Path(repo_root).expanduser().resolve()
    return {
        "mnemos-wiki": root / "wiki",
        "mnemos-runs": root / ".agent" / "runs",
        "mnemos-sessions": root / ".agent" / "sessions",
        "mnemos-transient": root / ".agent" / "transient",
    }


def _build_qmd_adapter(repo_root: str | Path) -> tuple[QmdAdapter, dict[str, Path]]:
    from core.gateway import MemoryGateway

    root = Path(repo_root).expanduser().resolve()
    gateway = MemoryGateway(repo_root=str(root))
    config = get_qmd_config(str(root))
    adapter = QmdAdapter(repo_root=root, store=gateway._store, config=config)
    collections = qmd_collection_roots(repo_root=root, store=gateway._store)
    return adapter, collections


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, QmdCommandError):
        return exc.code
    return exc.__class__.__name__


def _recover_processing_jobs(processing_dir: Path, pending_dir: Path) -> int:
    recovered = 0
    for path in sorted(processing_dir.glob("*.json")):
        try:
            path.replace(pending_dir / path.name)
        except FileNotFoundError:
            continue
        recovered += 1
    return recovered


def process_qmd_refresh(
    *,
    repo_root: str | Path,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> QmdRefreshResult:
    """Claim one pending batch and refresh the full derived QMD index once."""
    root = _queue_root(repo_root)
    pending_dir = root / "pending"
    processing_dir = root / "processing"
    done_dir = root / "done"
    failed_dir = root / "failed"
    for directory in (pending_dir, processing_dir, done_dir, failed_dir):
        directory.mkdir(parents=True, exist_ok=True)

    lease = _acquire_worker_lease(root)
    if lease is None:
        return QmdRefreshResult(processed=0, failed=0, skipped_locked=True)
    _write_worker_status(root, state="running", pid=os.getpid())

    processed = 0
    failed = 0
    retried = 0
    recovered = 0
    valid_jobs: list[tuple[Path, dict[str, Any]]] = []
    try:
        recovered = _recover_processing_jobs(processing_dir, pending_dir)
        for pending_path in sorted(pending_dir.glob("*.json")):
            processing_path = processing_dir / pending_path.name
            try:
                pending_path.replace(processing_path)
            except FileNotFoundError:
                continue
            try:
                payload = _read_json(processing_path)
                if not payload.get("job_id"):
                    raise ValueError("qmd refresh job_id is required")
                payload["reason"] = _bounded_reason(payload.get("reason"))
                _write_json_atomic(processing_path, payload)
            except Exception as exc:
                _write_json_atomic(
                    failed_dir / processing_path.name,
                    {
                        "job_id": processing_path.stem,
                        "failed_at": _now_iso(),
                        "error_code": exc.__class__.__name__,
                    },
                )
                processing_path.unlink(missing_ok=True)
                failed += 1
                continue
            valid_jobs.append((processing_path, payload))

        if valid_jobs:
            try:
                adapter, collections = _build_qmd_adapter(repo_root)
                adapter.update_index(collections)
            except Exception as exc:
                error_code = _failure_code(exc)
                max_attempts = max(1, int(max_attempts))
                for processing_path, payload in valid_jobs:
                    try:
                        attempts = int(payload.get("attempts", 0)) + 1
                    except (TypeError, ValueError):
                        attempts = 1
                    payload["attempts"] = attempts
                    payload["last_failed_at"] = _now_iso()
                    payload["error_code"] = error_code
                    if attempts < max_attempts:
                        destination = pending_dir / processing_path.name
                        retried += 1
                    else:
                        payload["failed_at"] = _now_iso()
                        destination = failed_dir / processing_path.name
                        failed += 1
                    _write_json_atomic(destination, payload)
                    processing_path.unlink(missing_ok=True)
            else:
                for processing_path, _payload in valid_jobs:
                    processing_path.replace(done_dir / processing_path.name)
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

    return QmdRefreshResult(
        processed=processed,
        failed=failed,
        retried=retried,
        recovered=recovered,
    )


def drain_qmd_refresh(
    *,
    repo_root: str | Path,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> QmdRefreshResult:
    """Drain jobs, including work enqueued while a previous batch was active."""
    processed = 0
    failed = 0
    retried = 0
    recovered = 0
    while True:
        result = process_qmd_refresh(
            repo_root=repo_root,
            max_attempts=max_attempts,
        )
        processed += result.processed
        failed += result.failed
        retried += result.retried
        recovered += result.recovered
        if result.skipped_locked:
            return QmdRefreshResult(
                processed=processed,
                failed=failed,
                retried=retried,
                recovered=recovered,
                skipped_locked=True,
            )
        if not any((_queue_root(repo_root) / "pending").glob("*.json")):
            return QmdRefreshResult(
                processed=processed,
                failed=failed,
                retried=retried,
                recovered=recovered,
            )
        time.sleep(0.1)


def get_qmd_refresh_diagnostics(*, repo_root: str | Path) -> dict[str, Any]:
    """Return content-free queue depth, age, and worker state."""
    _reap_detached_workers()
    root = _queue_root(repo_root)
    pending = list((root / "pending").glob("*.json"))
    processing = list((root / "processing").glob("*.json"))
    failed = list((root / "failed").glob("*.json"))
    done = list((root / "done").glob("*.json"))

    oldest_age: float | None = None
    mtimes: list[float] = []
    for path in pending:
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    if mtimes:
        oldest_age = round(max(0.0, time.time() - min(mtimes)), 3)

    worker: dict[str, Any] = {"state": "idle", "pid": None, "error_code": None}
    lock_path = root / "worker.lock"
    if lock_path.exists():
        try:
            owner = _read_json(lock_path / "owner.json")
            pid = int(owner["pid"])
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            worker["state"] = "stale"
        else:
            worker["state"] = "running" if _pid_is_running(pid) else "stale"
            worker["pid"] = pid
    else:
        try:
            status = _read_json(_worker_status_path(root))
        except (OSError, TypeError, json.JSONDecodeError):
            status = {}
        state = str(status.get("state") or "idle")
        pid_value = status.get("pid")
        pid = pid_value if isinstance(pid_value, int) else None
        if state in {"launching", "starting", "running"} and not (
            pid is not None and _pid_is_running(pid)
        ):
            state = "stale"
        worker["state"] = state
        worker["pid"] = pid
        error_code = status.get("error_code")
        worker["error_code"] = str(error_code) if error_code else None

    return {
        "pending": len(pending),
        "processing": len(processing),
        "failed": len(failed),
        "done": len(done),
        "oldest_pending_age_seconds": oldest_age,
        "worker": worker,
    }


def retry_failed_qmd_refresh(*, repo_root: str | Path) -> int:
    """Atomically requeue valid terminal failures with a fresh retry budget."""
    root = _queue_root(repo_root)
    pending_dir = root / "pending"
    failed_dir = root / "failed"
    pending_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)

    lease = _acquire_worker_lease(root)
    if lease is None:
        return 0
    requeued = 0
    try:
        for failed_path in sorted(failed_dir.glob("*.json")):
            try:
                payload = _read_json(failed_path)
            except (OSError, TypeError, json.JSONDecodeError):
                continue
            job_id = payload.get("job_id")
            reason = payload.get("reason")
            if not isinstance(job_id, str) or not job_id:
                continue
            if not isinstance(reason, str) or not reason:
                continue

            payload["reason"] = _bounded_reason(reason)
            payload["attempts"] = 0
            payload["requeued_at"] = _now_iso()
            for key in ("failed_at", "last_failed_at", "error_code"):
                payload.pop(key, None)
            _write_json_atomic(pending_dir / failed_path.name, payload)
            failed_path.unlink(missing_ok=True)
            requeued += 1
    finally:
        _release_worker_lease(lease)
    return requeued


def worker_entrypoint() -> int:
    """Process queued QMD refresh jobs in a detached worker process."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", ".")
    root = _queue_root(repo_root)
    try:
        result = drain_qmd_refresh(repo_root=repo_root)
        return 1 if result.failed else 0
    except Exception as exc:
        _write_worker_status(
            root,
            state="failed",
            pid=os.getpid(),
            error_code=exc.__class__.__name__,
        )
        return 1
