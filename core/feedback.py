"""Append-only memory feedback events and usage projection."""
from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


FEEDBACK_REQUEST_SCHEMA = "mnemos.feedback.request.v1"
ALLOWED_FEEDBACK_EVENTS = {
    "retrieved",
    "selected",
    "accepted",
    "applied",
    "validated",
    "ignored",
    "superseded",
    "invalidated",
}


class FeedbackValidationError(ValueError):
    """Raised when a feedback request violates the provider contract."""


class InactiveMemoryFeedbackError(FeedbackValidationError):
    """Raised when new use feedback targets a retired memory."""


def inactive_for_use(item: dict[str, Any] | None) -> bool:
    return bool(item and (item.get("stage") == "archived"
                or item.get("semantic_status") in {"deprecated", "invalidated", "superseded"}
                or item.get("superseded_by")))


def ledger_retired(events: list[dict[str, Any]], memory_id: str) -> bool:
    return any(row.get("memory_id") == memory_id and row.get("event") in {"invalidated", "superseded"} for row in events)


class FeedbackStore:
    """Persist feedback events separately from memory item files."""

    def __init__(self, repo_root: str | Path) -> None:
        self._root = Path(repo_root)
        self._feedback_dir = self._root / ".agent" / "feedback"
        self.ledger_path = self._feedback_dir / "events.jsonl"
        self.projection_path = self._feedback_dir / "usage_projection.json"

    def record(self, request: dict[str, Any], *, legacy_access_count: int = 0,
               current_item: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = normalize_feedback_request(request)
        self._feedback_dir.mkdir(parents=True, exist_ok=True)
        # The transaction serializes the retirement check and append across CLI processes.
        with closing(sqlite3.connect(self._feedback_dir / "write-lock.sqlite3")) as lock:
            lock.execute("BEGIN IMMEDIATE")
            return self._record(normalized, legacy_access_count=legacy_access_count, current_item=current_item)

    def _record(self, normalized: dict[str, Any], *, legacy_access_count: int,
                current_item: dict[str, Any] | None) -> dict[str, Any]:
        existing_events = self.read_events()
        duplicate = _find_duplicate(existing_events, normalized)
        memory_id = normalized["memory_id"]
        if duplicate is None:
            if normalized["event"] in {"applied", "validated"} and (
                inactive_for_use(current_item) or ledger_retired(existing_events, memory_id)
            ):
                raise InactiveMemoryFeedbackError(f"memory '{memory_id}' is no longer eligible for use")
            entry = {
                **normalized,
                "recorded_at": _now_iso(),
                "legacy_access_count": legacy_access_count,
            }
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            events = [*existing_events, entry]
            accepted_event = entry
        else:
            events = existing_events
            accepted_event = duplicate

        projection = build_usage_projection(events)
        self._write_projection(projection)

        return {
            "event": {
                "event_id": normalized["event_id"],
                "event": normalized["event"],
                "memory_id": memory_id,
                "duplicate": duplicate is not None,
                "recorded_at": accepted_event.get("recorded_at"),
                "idempotency_key": normalized["idempotency_key"],
            },
            "projection": projection.get(memory_id, empty_usage_projection(memory_id, legacy_access_count)),
            "ledger_path": str(self.ledger_path),
            "projection_path": str(self.projection_path),
        }

    def read_events(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []

        events: list[dict[str, Any]] = []
        with self.ledger_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    events.append(entry)
        return events

    def read_projection(self) -> dict[str, dict[str, Any]]:
        if not self.projection_path.exists():
            return build_usage_projection(self.read_events())

        try:
            payload = json.loads(self.projection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return build_usage_projection(self.read_events())
        if not isinstance(payload, dict):
            return {}

        projection = {
            str(memory_id): dict(value)
            for memory_id, value in payload.items()
            if isinstance(value, dict)
        }
        if self.ledger_path.is_file() and any(
            value.get("applied_count") and "candidate_applied_count" not in value
            for value in projection.values()
        ):
            return build_usage_projection(self.read_events())

        return projection

    def _write_projection(self, projection: dict[str, dict[str, Any]]) -> None:
        self._feedback_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self.projection_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(projection, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.projection_path)


def normalize_feedback_request(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("schema_version") != FEEDBACK_REQUEST_SCHEMA:
        raise FeedbackValidationError(f"schema_version must be {FEEDBACK_REQUEST_SCHEMA}")

    event_id = _required_string(request, "event_id")
    event = _required_string(request, "event")
    if event not in ALLOWED_FEEDBACK_EVENTS:
        raise FeedbackValidationError(f"event must be one of {sorted(ALLOWED_FEEDBACK_EVENTS)}")

    memory_id = _required_string(request, "memory_id")
    task_id = _required_string(request, "task_id")
    application = request.get("application") if isinstance(request.get("application"), dict) else {}
    artifact = _optional_string(application.get("artifact"))
    locator_type = _optional_string(application.get("locator_type"))
    locator = _optional_string(application.get("locator"))
    effect = _optional_string(application.get("effect"))
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}

    normalized = {
        "schema_version": FEEDBACK_REQUEST_SCHEMA,
        "event_id": event_id,
        "event": event,
        "memory_id": memory_id,
        "task_id": task_id,
        "project_id": _optional_string(request.get("project_id")),
        "agent_role": _optional_string(request.get("agent_role")),
        "application": {
            "artifact": artifact,
            "locator_type": locator_type,
            "locator": locator,
            "effect": effect,
        },
        "reason_code": _optional_string(request.get("reason_code")),
        "metadata": dict(metadata),
    }
    normalized["idempotency_key"] = _secondary_key(normalized)
    return normalized


def build_usage_projection(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    projection: dict[str, dict[str, Any]] = {}
    applied_tasks: dict[str, set[str]] = {}
    validated_tasks: dict[str, set[str]] = {}
    applied_projects: dict[str, set[str]] = {}
    validated_projects: dict[str, set[str]] = {}

    for entry in events:
        memory_id = str(entry.get("memory_id") or "")
        if not memory_id:
            continue

        usage = projection.setdefault(
            memory_id,
            empty_usage_projection(memory_id, _int_value(entry.get("legacy_access_count"), 0)),
        )
        usage["legacy_access_count"] = _int_value(
            usage.get("legacy_access_count"),
            _int_value(entry.get("legacy_access_count"), 0),
        )

        event = entry.get("event")
        ts = entry.get("recorded_at")
        task_id = str(entry.get("task_id") or "")
        project_id = str(entry.get("project_id") or "")
        if event == "retrieved":
            usage["retrieval_count"] += 1
            usage["last_retrieved_at"] = _latest_ts(usage["last_retrieved_at"], ts)
        elif event == "selected":
            usage["selected_count"] += 1
        elif event == "applied":
            usage["applied_count"] += 1
            if entry.get("reason_code") == "source_reviewed_candidate":
                usage["candidate_applied_count"] += 1
            usage["last_applied_at"] = _latest_ts(usage["last_applied_at"], ts)
            if task_id:
                applied_tasks.setdefault(memory_id, set()).add(task_id)
            if project_id:
                applied_projects.setdefault(memory_id, set()).add(project_id)
        elif event == "validated":
            usage["validated_use_count"] += 1
            usage["last_validated_at"] = _latest_ts(usage["last_validated_at"], ts)
            metadata = entry.get("metadata")
            binding = metadata.get("reviewed_source_binding") if isinstance(metadata, dict) else None
            if (entry.get("reason_code") == "source_reviewed_candidate" and isinstance(binding, str)
                    and len(binding) == 64 and all(char in "0123456789abcdef" for char in binding)):
                bindings = usage.setdefault("reviewed_source_bindings", [])
                if binding not in bindings:
                    bindings.append(binding)
            if task_id:
                validated_tasks.setdefault(memory_id, set()).add(task_id)
            if project_id:
                validated_projects.setdefault(memory_id, set()).add(project_id)
        elif event == "invalidated":
            usage["invalidated"] = True
        elif event == "superseded":
            usage["superseded"] = True

    for memory_id, usage in projection.items():
        usage["distinct_applied_task_count"] = len(applied_tasks.get(memory_id, set()))
        usage["distinct_validated_task_count"] = len(validated_tasks.get(memory_id, set()))
        usage["distinct_applied_project_count"] = len(applied_projects.get(memory_id, set()))
        usage["distinct_validated_project_count"] = len(validated_projects.get(memory_id, set()))

    return projection


def empty_usage_projection(memory_id: str, legacy_access_count: int = 0) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "retrieval_count": 0,
        "selected_count": 0,
        "applied_count": 0,
        "candidate_applied_count": 0,
        "validated_use_count": 0,
        "distinct_applied_task_count": 0,
        "distinct_validated_task_count": 0,
        "distinct_applied_project_count": 0,
        "distinct_validated_project_count": 0,
        "last_retrieved_at": None,
        "last_applied_at": None,
        "last_validated_at": None,
        "legacy_access_count": legacy_access_count,
        "invalidated": False,
        "superseded": False,
    }


def validated_usage_score(usage: dict[str, Any] | None, *, include_applied: bool = True) -> float:
    """Return Recall-only usage score from applied and validated feedback."""
    if not usage:
        return 0.0

    ranked_applications = _int_value(usage.get("applied_count"), 0) - _int_value(usage.get("candidate_applied_count"), 0)
    applied = min(1.0, max(0.0, ranked_applications / 4)) if include_applied else 0.0
    validated = min(1.0, _int_value(usage.get("validated_use_count"), 0) / 3)
    distinct_validated = min(1.0, _int_value(usage.get("distinct_validated_task_count"), 0) / 2)
    return round((applied * 0.2) + (validated * 0.3) + (distinct_validated * 0.5), 6)


def is_recall_suppressed(usage: dict[str, Any] | None) -> bool:
    """Return True when feedback says a memory should not appear by default."""
    if not usage:
        return False
    return bool(usage.get("invalidated") or usage.get("superseded"))


def _find_duplicate(
    events: list[dict[str, Any]],
    normalized: dict[str, Any],
) -> dict[str, Any] | None:
    event_id = normalized["event_id"]
    idempotency_key = normalized["idempotency_key"]
    for entry in events:
        if entry.get("event_id") == event_id:
            return entry

    for entry in events:
        if entry.get("idempotency_key") == idempotency_key:
            return entry
    return None


def _secondary_key(normalized: dict[str, Any]) -> str:
    application = normalized.get("application") or {}
    return "|".join([
        str(normalized.get("task_id") or ""),
        str(normalized.get("memory_id") or ""),
        str(normalized.get("event") or ""),
        str(application.get("artifact") or ""),
        str(application.get("locator") or ""),
    ])


def _required_string(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FeedbackValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_ts(current: Any, candidate: Any) -> str | None:
    if not isinstance(candidate, str) or not candidate:
        return current if isinstance(current, str) else None
    if not isinstance(current, str) or candidate > current:
        return candidate
    return current


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
