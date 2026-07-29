"""Tests for the read-only Recall provider CLI contract."""
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


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "mnemos.recall.request.v1",
        "request_id": "task-id-recall",
        "queries": [{"type": "literal", "text": "recall contract"}],
        "scope": {
            "project_id": None,
            "project_root_hash": None,
            "repository": None,
            "agent_role": None,
            "task_shape": None,
            "active_files": [],
        },
        "filters": {
            "layers": [],
            "semantic_statuses": [],
            "tags_all": [],
            "tags_any": [],
        },
        "budget": {
            "candidate_limit": 20,
            "selected_limit": 6,
            "max_selected_chars": 3600,
        },
        "read_only": True,
    }
    payload.update(overrides)
    return payload


def _write_request(tmp_path: Path, payload: dict[str, Any]) -> Path:
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path


def _capture(runner: CliRunner, content: str, item_id: str, **metadata: Any) -> None:
    from core.gateway import MemoryGateway

    gateway = MemoryGateway(repo_root=os.environ["MNEMOS_REPO_ROOT"])
    captured_id = gateway.capture(
        layer="project",
        content=content,
        item_id=item_id,
        extra_metadata=metadata or None,
        no_classify=True,
    )
    assert captured_id == item_id


def _hash_memory_files(repo_root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(repo_root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((repo_root / "wiki").rglob("*.md"))
        if path.name != "log.md"
    }


def test_recall_reads_file_request_and_returns_results(runner: CliRunner, tmp_path: Path) -> None:
    _capture(
        runner,
        "recall contract provider memory",
        "recall-cli-001",
        semantic_status="active",
        project_id="project-a",
    )
    request_path = _write_request(
        tmp_path,
        _request(
            scope={"project_id": "project-a"},
            filters={"layers": ["project"], "semantic_statuses": ["active"]},
        ),
    )

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "mnemos.recall.response.v1"
    assert payload["provider"] == "mnemos"
    assert payload["request_id"] == "task-id-recall"
    assert payload["status"] == "ok"
    assert payload["partial_failure"] is False
    assert payload["results"][0]["memory_id"] == "recall-cli-001"
    assert payload["results"][0]["retrieval_score"] is not None
    assert payload["selected_ids"] == ["recall-cli-001"]


def test_recall_reads_stdin_request(runner: CliRunner) -> None:
    _capture(runner, "stdin recall contract memory", "recall-stdin-001")
    request = json.dumps(_request(queries=[{"type": "literal", "text": "stdin recall"}]))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=request)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["results"][0]["memory_id"] == "recall-stdin-001"


def test_recall_invalid_json_returns_error_json(runner: CliRunner, tmp_path: Path) -> None:
    request_path = tmp_path / "bad.json"
    request_path.write_text("{bad", encoding="utf-8")

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_json"


def test_recall_rejects_empty_queries(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(tmp_path, _request(queries=[]))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "validation_error"


def test_recall_rejects_json_array_request(runner: CliRunner, tmp_path: Path) -> None:
    request_path = tmp_path / "array.json"
    request_path.write_text("[]", encoding="utf-8")

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "invalid_json"


def test_recall_rejects_queries_without_text(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(tmp_path, _request(queries=[{"type": "literal", "text": " "}, {}]))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "validation_error"


def test_recall_rejects_read_only_false(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(tmp_path, _request(read_only=False))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "validation_error"


def test_recall_accepts_string_query_and_ignores_malformed_optional_sections(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    _capture(runner, "string query recall memory", "recall-string-query")
    request_path = _write_request(
        tmp_path,
        _request(
            queries=["string query"],
            scope="unknown",
            filters="unknown",
            budget={
                "candidate_limit": "bad",
                "selected_limit": "bad",
                "max_selected_chars": "bad",
            },
        ),
    )

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["results"][0]["memory_id"] == "recall-string-query"


def test_recall_ok_with_zero_results_is_not_error(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(
        tmp_path,
        _request(queries=[{"type": "literal", "text": "no matching recall memory"}]),
    )

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["partial_failure"] is False
    assert payload["results"] == []
    assert payload["selected_ids"] == []


def test_recall_degraded_reports_fallback_or_backend_failure(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import cli as cli_module

    class DegradedGateway:
        last_search_diagnostics = {
            "partial_failure": True,
            "fallback_used": False,
            "degraded_reasons": ["vector: unavailable"],
            "backends": [{"name": "vector", "status": "unavailable"}],
        }

        def recall(self, **_kwargs: Any):
            from core.contracts import RecallReport

            return RecallReport(
                queries=("degraded",),
                candidates=(),
                selected=(),
                candidate_limit=20,
                selected_limit=6,
                max_selected_chars=3600,
                used_chars=0,
                diagnostics={"attempts": [self.last_search_diagnostics]},
            )

    monkeypatch.setattr(cli_module, "_get_gateway", lambda: DegradedGateway())
    request_path = _write_request(tmp_path, _request(queries=[{"type": "literal", "text": "degraded"}]))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "degraded"
    assert payload["partial_failure"] is True
    assert payload["diagnostics"]["degraded_reasons"] == ["vector: unavailable"]


def test_recall_degraded_reports_partial_failure_and_fallback_once(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import cli as cli_module

    class FallbackGateway:
        def recall(self, **_kwargs: Any):
            from core.contracts import RecallReport

            attempt = {
                "partial_failure": True,
                "fallback_used": True,
                "degraded_reasons": [],
                "backends": [
                    {"name": "grep", "status": "used"},
                    {"name": "grep", "status": "used"},
                ],
            }
            return RecallReport(
                queries=("fallback",),
                candidates=(),
                selected=(),
                candidate_limit=20,
                selected_limit=6,
                max_selected_chars=3600,
                used_chars=0,
                diagnostics={"attempts": [attempt]},
            )

    monkeypatch.setattr(cli_module, "_get_gateway", lambda: FallbackGateway())
    request_path = _write_request(tmp_path, _request(queries=[{"type": "literal", "text": "fallback"}]))

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "degraded"
    assert payload["diagnostics"]["fallback_used"] is True
    assert payload["diagnostics"]["degraded_reasons"] == [
        "retrieval_backend_partial_failure",
        "retrieval_fallback_used",
    ]
    assert payload["diagnostics"]["backends"] == [{"name": "grep", "status": "used"}]


def test_recall_fatal_backend_error_returns_error_json(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import cli as cli_module

    class BrokenGateway:
        def recall(self, **_kwargs: Any):
            raise RuntimeError("vault unavailable")

    monkeypatch.setattr(cli_module, "_get_gateway", lambda: BrokenGateway())
    request_path = _write_request(tmp_path, _request())

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "backend_error"


def test_recall_stdout_is_json_only(runner: CliRunner, tmp_path: Path) -> None:
    request_path = _write_request(tmp_path, _request())

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    assert result.output.lstrip().startswith("{")
    assert result.output.rstrip().endswith("}")
    json.loads(result.output)


def test_recall_does_not_fabricate_retrieval_score_when_unsupported(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import cli as cli_module

    class ScorelessMemory:
        id = "scoreless"
        content = "scoreless recall"
        layer = "project"
        semantic_status = "active"
        tags = ()
        task_shape = None
        project_id = None
        project_root_hash = None
        created_at = None
        updated_at = None
        score = None

    class ScorelessGateway:
        def recall(self, **_kwargs: Any):
            from core.contracts import RecallReport

            memory = ScorelessMemory()
            return RecallReport(
                queries=("scoreless",),
                candidates=(memory,),
                selected=(memory,),
                candidate_limit=20,
                selected_limit=6,
                max_selected_chars=3600,
                used_chars=16,
                diagnostics={"attempts": []},
            )

    monkeypatch.setattr(cli_module, "_get_gateway", lambda: ScorelessGateway())
    request_path = _write_request(tmp_path, _request())

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["results"][0]["retrieval_score"] is None
    assert "retrieval_score_unavailable" in payload["diagnostics"]["degraded_reasons"]


def test_recall_result_scores_for_multiple_results(runner: CliRunner, tmp_path: Path) -> None:
    _capture(runner, "multi score recall one", "recall-score-001")
    _capture(runner, "multi score recall two", "recall-score-002")
    request_path = _write_request(
        tmp_path,
        _request(
            queries=[{"type": "literal", "text": "multi score recall"}],
            budget={"candidate_limit": 2, "selected_limit": 1, "max_selected_chars": 3600},
        ),
    )

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["rank_score"] for item in payload["results"]] == [1.0, 0.0]
    assert payload["results"][0]["context_score"] is not None
    assert payload["results"][1]["context_score"] is None


def test_recall_provider_helper_edge_cases() -> None:
    from core.provider import _append_unique, _optional_float

    assert _optional_float(object()) is None
    values = ["existing"]
    seen = {"existing"}
    _append_unique(values, seen, "existing")
    assert values == ["existing"]


def test_capabilities_expose_recall_contract(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capabilities"]["recall_v1"] is True
    assert payload["capabilities"]["recall_read_only"] is True
    assert payload["capabilities"]["retrieval_score"] is True
    assert payload["capabilities"]["project_scope_filter"] is True
    assert "recall_scores" in payload
    assert payload["recall_scores"]["retrieval_score"]


def test_recall_cli_keeps_memory_files_unchanged(
    runner: CliRunner,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _capture(runner, "unchanged recall memory", "recall-unchanged-001")
    before_hashes = _hash_memory_files(repo_root)
    request_path = _write_request(
        tmp_path,
        _request(queries=[{"type": "literal", "text": "unchanged recall"}]),
    )

    result = runner.invoke(cli, ["recall", "--json", "--request-file", str(request_path)])

    assert result.exit_code == 0, result.output
    assert _hash_memory_files(repo_root) == before_hashes
