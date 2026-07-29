"""Tests for the memory Feedback provider CLI contract."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for dirname in ["runs", "sessions", "state", "reports", "tools", "transient"]:
        (agent / dirname).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _isolate_repo(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "none")


def _capture_memory(memory_id: str, content: str = "feedback target memory") -> None:
    from core.gateway import MemoryGateway

    gateway = MemoryGateway(repo_root=os.environ["MNEMOS_REPO_ROOT"])
    captured_id = gateway.capture(
        layer="project",
        content=content,
        item_id=memory_id,
        no_classify=True,
    )
    assert captured_id == memory_id


def _feedback_request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "mnemos.feedback.request.v1",
        "event_id": "event-001",
        "event": "applied",
        "memory_id": "mem-123",
        "task_id": "task-001",
        "project_id": "agent-crew",
        "agent_role": "analyst",
        "application": {
            "artifact": "pipeline.json",
            "locator_type": "json_pointer",
            "locator": "/stages/0/tdd_parallel",
            "effect": "set_true",
        },
        "reason_code": "matched_prior_aar",
        "metadata": {},
    }
    payload.update(overrides)
    return payload


def _write_request(tmp_path: Path, payload: dict[str, Any]) -> Path:
    request_path = tmp_path / "feedback.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path


def _invoke_feedback(runner: CliRunner, request_path: Path | str, input_text: str | None = None):
    return runner.invoke(cli, ["feedback", "--json", "--request-file", str(request_path)], input=input_text)


def _hash_memory_files(repo_root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(repo_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((repo_root / "wiki").rglob("*.md"))
        if path.name != "log.md"
    }


def test_feedback_reads_file_request_and_updates_projection(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    _capture_memory("mem-123")
    request_path = _write_request(tmp_path, _feedback_request())

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "mnemos.feedback.response.v1"
    assert payload["status"] == "ok"
    assert payload["event"]["event_id"] == "event-001"
    assert payload["event"]["duplicate"] is False
    assert payload["projection"]["memory_id"] == "mem-123"
    assert payload["projection"]["applied_count"] == 1
    assert payload["projection"]["validated_use_count"] == 0
    assert payload["projection"]["legacy_access_count"] == 0


def test_feedback_reads_stdin_request(runner: CliRunner) -> None:
    _capture_memory("mem-stdin")
    request = json.dumps(_feedback_request(event_id="stdin-001", memory_id="mem-stdin"))

    result = _invoke_feedback(runner, "-", input_text=request)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["projection"]["memory_id"] == "mem-stdin"
    assert payload["projection"]["applied_count"] == 1


def test_feedback_event_id_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    _capture_memory("mem-idem")
    request_path = _write_request(tmp_path, _feedback_request(event_id="same-event", memory_id="mem-idem"))

    first = _invoke_feedback(runner, request_path)
    second = _invoke_feedback(runner, request_path)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["event"]["duplicate"] is True
    assert payload["projection"]["applied_count"] == 1


def test_feedback_secondary_key_is_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    _capture_memory("mem-secondary")
    base = _feedback_request(event_id="event-a", memory_id="mem-secondary")
    duplicate = _feedback_request(event_id="event-b", memory_id="mem-secondary")
    first_path = _write_request(tmp_path, base)
    second_path = tmp_path / "feedback-2.json"
    second_path.write_text(json.dumps(duplicate), encoding="utf-8")

    first = _invoke_feedback(runner, first_path)
    second = _invoke_feedback(runner, second_path)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    payload = json.loads(second.output)
    assert payload["event"]["duplicate"] is True
    assert payload["projection"]["applied_count"] == 1


def test_feedback_applied_and_validated_are_counted_separately(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    _capture_memory("mem-counts")
    applied_path = _write_request(tmp_path, _feedback_request(event_id="applied-1", memory_id="mem-counts"))
    validated_path = tmp_path / "validated.json"
    validated_path.write_text(
        json.dumps(_feedback_request(event_id="validated-1", event="validated", memory_id="mem-counts")),
        encoding="utf-8",
    )

    assert _invoke_feedback(runner, applied_path).exit_code == 0
    result = _invoke_feedback(runner, validated_path)

    assert result.exit_code == 0, result.output
    projection = json.loads(result.output)["projection"]
    assert projection["applied_count"] == 1
    assert projection["validated_use_count"] == 1
    assert projection["distinct_applied_task_count"] == 1
    assert projection["distinct_validated_task_count"] == 1
    assert projection["last_applied_at"]
    assert projection["last_validated_at"]


def test_feedback_ignored_does_not_increment_applied_count(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    _capture_memory("mem-ignored")
    request_path = _write_request(
        tmp_path,
        _feedback_request(event_id="ignored-1", event="ignored", memory_id="mem-ignored"),
    )

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 0, result.output
    projection = json.loads(result.output)["projection"]
    assert projection["applied_count"] == 0
    assert projection["validated_use_count"] == 0


def test_feedback_distinct_task_counts(runner: CliRunner, tmp_path: Path) -> None:
    _capture_memory("mem-tasks")
    first_path = _write_request(tmp_path, _feedback_request(event_id="task-a", memory_id="mem-tasks", task_id="task-a"))
    second_path = tmp_path / "task-b.json"
    second_path.write_text(
        json.dumps(_feedback_request(event_id="task-b", memory_id="mem-tasks", task_id="task-b")),
        encoding="utf-8",
    )

    assert _invoke_feedback(runner, first_path).exit_code == 0
    result = _invoke_feedback(runner, second_path)

    assert result.exit_code == 0, result.output
    projection = json.loads(result.output)["projection"]
    assert projection["applied_count"] == 2
    assert projection["distinct_applied_task_count"] == 2


def test_feedback_retrieved_and_selected_counts_are_separate(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    _capture_memory("mem-surfaced")
    retrieved_path = _write_request(
        tmp_path,
        _feedback_request(event_id="retrieved-1", event="retrieved", memory_id="mem-surfaced"),
    )
    selected_path = tmp_path / "selected.json"
    selected_path.write_text(
        json.dumps(_feedback_request(event_id="selected-1", event="selected", memory_id="mem-surfaced")),
        encoding="utf-8",
    )

    assert _invoke_feedback(runner, retrieved_path).exit_code == 0
    result = _invoke_feedback(runner, selected_path)

    assert result.exit_code == 0, result.output
    projection = json.loads(result.output)["projection"]
    assert projection["retrieval_count"] == 1
    assert projection["selected_count"] == 1
    assert projection["applied_count"] == 0
    assert projection["last_retrieved_at"]


def test_feedback_rejects_invalid_memory_id(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(tmp_path, _feedback_request(memory_id="missing-memory"))

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_memory_id"


def test_feedback_rejects_invalid_event(runner: CliRunner, tmp_path: Path) -> None:
    _capture_memory("mem-invalid-event")
    request_path = _write_request(
        tmp_path,
        _feedback_request(event_id="bad-event", event="opened", memory_id="mem-invalid-event"),
    )

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "validation_error"


def test_feedback_invalid_json_returns_error_json(runner: CliRunner, tmp_path: Path) -> None:
    request_path = tmp_path / "bad.json"
    request_path.write_text("{bad", encoding="utf-8")

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_json"


def test_feedback_ledger_and_projection_are_consistent(
    runner: CliRunner,
    tmp_path: Path,
    repo_root: Path,
) -> None:
    _capture_memory("mem-ledger")
    request_path = _write_request(tmp_path, _feedback_request(event_id="ledger-1", memory_id="mem-ledger"))

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ledger_path = repo_root / ".agent" / "feedback" / "events.jsonl"
    projection_path = repo_root / ".agent" / "feedback" / "usage_projection.json"
    ledger_entries = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    assert len(ledger_entries) == 1
    assert ledger_entries[0]["event_id"] == "ledger-1"
    assert projection["mem-ledger"] == payload["projection"]


def test_feedback_does_not_modify_memory_content(runner: CliRunner, tmp_path: Path, repo_root: Path) -> None:
    _capture_memory("mem-unchanged")
    before_hashes = _hash_memory_files(repo_root)
    request_path = _write_request(tmp_path, _feedback_request(event_id="unchanged-1", memory_id="mem-unchanged"))

    result = _invoke_feedback(runner, request_path)

    assert result.exit_code == 0, result.output
    assert _hash_memory_files(repo_root) == before_hashes


def test_feedback_helper_edge_cases(tmp_path: Path) -> None:
    from core.feedback import (
        FEEDBACK_REQUEST_SCHEMA,
        FeedbackStore,
        FeedbackValidationError,
        build_usage_projection,
        normalize_feedback_request,
    )

    store = FeedbackStore(tmp_path)
    assert store.read_events() == []
    store.ledger_path.parent.mkdir(parents=True)
    store.ledger_path.write_text("\n{bad\n[]\n", encoding="utf-8")
    assert store.read_events() == []

    with pytest.raises(FeedbackValidationError):
        normalize_feedback_request({"schema_version": "wrong"})
    with pytest.raises(FeedbackValidationError):
        normalize_feedback_request({"schema_version": FEEDBACK_REQUEST_SCHEMA, "event_id": " "})

    projection = build_usage_projection([
        {"memory_id": "", "event": "applied"},
        {"memory_id": "mem-edge", "event": "accepted", "legacy_access_count": "bad"},
        {"memory_id": "mem-edge", "event": "retrieved", "recorded_at": None},
    ])
    assert projection["mem-edge"]["legacy_access_count"] == 0
    assert projection["mem-edge"]["retrieval_count"] == 1
    assert projection["mem-edge"]["last_retrieved_at"] is None


def test_capabilities_expose_feedback_contract(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capabilities"]["feedback_v1"] == "supported"
    assert payload["capability_status"]["feedback_v1"] == "supported"
