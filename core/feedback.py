"""Append-only memory feedback events and usage projection."""
from __future__ import annotations

import datetime
import json
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


class FeedbackStore:
    """Persist feedback events separately from memory item files."""

    def __init__(self, repo_root: str | Path) -> None:
        self._root = Path(repo_root)
        self._feedback_dir = self._root / ".agent" / "feedback"
        self.ledger_path = self._feedback_dir / "events.jsonl"
        self.projection_path = self._feedback_dir / "usage_projection.json"

    def record(self, request: dict[str, Any], *, legacy_access_count: int = 0) -> dict[str, Any]:
        normalized = normalize_feedback_request(request)
        self._ensure_files()

        existing_events = self.read_events()
        duplicate = _find_duplicate(existing_events, normalized)
        if duplicate is None:
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

        memory_id = normalized["memory_id"]
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

    def _ensure_files(self) -> None:
        self._feedback_dir.mkdir(parents=True, exist_ok=True)
        if not self.ledger_path.exists():
            self.ledger_path.touch()
        if not self.projection_path.exists():
            self._write_projection({})

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
        if event == "retrieved":
            usage["retrieval_count"] += 1
            usage["last_retrieved_at"] = _latest_ts(usage["last_retrieved_at"], ts)
        elif event == "selected":
            usage["selected_count"] += 1
        elif event == "applied":
            usage["applied_count"] += 1
            usage["last_applied_at"] = _latest_ts(usage["last_applied_at"], ts)
            if task_id:
                applied_tasks.setdefault(memory_id, set()).add(task_id)
        elif event == "validated":
            usage["validated_use_count"] += 1
            usage["last_validated_at"] = _latest_ts(usage["last_validated_at"], ts)
            if task_id:
                validated_tasks.setdefault(memory_id, set()).add(task_id)

    for memory_id, usage in projection.items():
        usage["distinct_applied_task_count"] = len(applied_tasks.get(memory_id, set()))
        usage["distinct_validated_task_count"] = len(validated_tasks.get(memory_id, set()))

    return projection


def empty_usage_projection(memory_id: str, legacy_access_count: int = 0) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "retrieval_count": 0,
        "selected_count": 0,
        "applied_count": 0,
        "validated_use_count": 0,
        "distinct_applied_task_count": 0,
        "distinct_validated_task_count": 0,
        "last_retrieved_at": None,
        "last_applied_at": None,
        "last_validated_at": None,
        "legacy_access_count": legacy_access_count,
    }


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
