"""Durable asynchronous capture queue."""
from __future__ import annotations

import datetime
import json
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


@dataclass(frozen=True)
class CaptureQueueResult:
    processed: int
    failed: int


def _queue_root(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".agent" / "state" / "capture-queue"


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
    pending_dir = _queue_root(repo_root) / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    job_path = pending_dir / f"{resolved_id}.json"
    tmp_path = job_path.with_suffix(".json.tmp")
    payload: dict[str, Any] = {
        "item_id": resolved_id,
        "content": content,
        "layer": layer,
        "tags": tags or [],
        "quality_score": quality_score,
        "run_id": run_id,
        "session_id": session_id,
        "no_classify": no_classify,
        "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(job_path)
    return QueuedCapture(item_id=resolved_id, status="queued", path=job_path)


def process_pending_captures(*, repo_root: str | Path, limit: int | None = None) -> CaptureQueueResult:
    """Materialize pending capture jobs through the normal MemoryGateway path."""
    root = _queue_root(repo_root)
    pending_dir = root / "pending"
    processing_dir = root / "processing"
    done_dir = root / "done"
    failed_dir = root / "failed"
    processing_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    if not pending_dir.exists():
        return CaptureQueueResult(processed=0, failed=0)

    attempted = 0
    processed = 0
    failed = 0
    gateway = MemoryGateway(repo_root=str(repo_root))
    for job_path in sorted(pending_dir.glob("*.json")):
        if limit is not None and attempted >= limit:
            break
        processing_path = processing_dir / job_path.name
        try:
            job_path.replace(processing_path)
        except FileNotFoundError:
            continue

        attempted += 1
        try:
            payload = json.loads(processing_path.read_text(encoding="utf-8"))
            gateway.capture(
                content=payload["content"],
                layer=payload.get("layer"),
                tags=list(payload.get("tags") or []),
                quality_score=float(payload.get("quality_score", 0.8)),
                run_id=payload.get("run_id"),
                session_id=payload.get("session_id"),
                item_id=payload["item_id"],
                no_classify=bool(payload.get("no_classify", False)),
            )
            processing_path.replace(done_dir / processing_path.name)
            processed += 1
        except Exception as exc:
            failed += 1
            fail_path = failed_dir / processing_path.name
            try:
                payload = json.loads(processing_path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            payload["failed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            payload["error"] = f"{exc.__class__.__name__}: {exc}"
            fail_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            processing_path.unlink(missing_ok=True)

    return CaptureQueueResult(processed=processed, failed=failed)
