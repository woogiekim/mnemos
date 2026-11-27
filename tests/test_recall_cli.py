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


def test_recall_exposes_project_context_source_location(runner: CliRunner, repo_root: Path) -> None:
    source = repo_root / "decisions.md"
    source.write_text("# Worktree baseline\n\nKeep the original worktree baseline.\n", encoding="utf-8")

    captured = runner.invoke(cli, [
        "project-context", "capture", str(source), "--project-root", str(repo_root),
        "--project-id", "project-a", "--json",
    ])
    assert captured.exit_code == 0, captured.output

    request = json.dumps(_request(
        queries=[{"type": "literal", "text": "worktree baseline"}],
        scope={"project_id": "project-a"},
        filters={"layers": ["project"], "semantic_statuses": ["active"]},
    ))
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=request)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    memory = payload["results"][0]
    assert payload["selected_ids"] == [memory["memory_id"]]
    assert memory["source_path"] == "decisions.md"
    assert memory["source_section"] == "Worktree baseline"
    assert memory["source_content_hash"] == hashlib.sha256(source.read_text().strip().encode("utf-8")).hexdigest()
    assert memory["semantic_status"] == "active"

    archived = runner.invoke(cli, ["archive", memory["memory_id"]])
    assert archived.exit_code == 0, archived.output

    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=request)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["selected_ids"] == []


def test_project_context_remains_scoped_after_direct_read_and_consolidation(runner: CliRunner, repo_root: Path) -> None:
    from core.gateway import MemoryGateway
    from core.policy import PolicyViolationError

    document = repo_root / "checks.md"
    document.write_text("# Verify checkpoint\n\nKeep the original checkpoint.\n", encoding="utf-8")
    captured = runner.invoke(cli, ["project-context", "capture", str(document), "--project-id", "project-a",
                                  "--project-root", str(repo_root), "--json"])
    assert captured.exit_code == 0, captured.output
    memory_id, = json.loads(captured.output)["created"]
    gateway = MemoryGateway(repo_root=str(repo_root))

    read = runner.invoke(cli, ["read", "--json", memory_id])
    assert read.exit_code == 0, read.output
    assert gateway.peek(memory_id)["layer"] == "project"
    assert gateway.peek(memory_id)["access_count"] == 1
    assert gateway.consolidate(run_distill=False) == 0
    with pytest.raises(PolicyViolationError, match="project-context"):
        gateway.promote(memory_id, target_layer="global", force=True)

    request = _request(queries=[{"type": "literal", "text": "Verify checkpoint"}],
                       scope={"project_context": {"project_id": "project-a",
                                                  "project_root_hash": gateway.peek(memory_id)["project_root_hash"]}},
                       filters={"layers": ["project"], "semantic_statuses": ["active"]})
    recalled = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
    assert recalled.exit_code == 0, recalled.output
    row, = json.loads(recalled.output)["results"]
    assert row["memory_id"] == memory_id and row["source_section"] == "Verify checkpoint"


def test_recall_includes_common_source_sections_only_with_exact_project_scope(runner: CliRunner) -> None:
    legacy_id = "project-a-0123456789"
    legacy_hash = "0123456789"
    source_hash = "0123456789abcdef"
    source = {
        "sourceType": "project_context_section",
        "source_path": "decisions.md",
        "source_section": "Shared checks",
        "project_id": "project-a",
        "project_root_hash": source_hash,
        "semantic_status": "active",
    }
    for item_id, metadata in [
        ("common", source),
        ("different-root", {**source, "project_root_hash": "0123456789ffff00"}),
        ("different-project", {**source, "project_id": "project-b"}),
        ("different-role", {**source, "agent_role": "backend"}),
        ("different-shape", {**source, "task_shape": "feature"}),
        ("no-source", {**source, "source_path": None}),
        ("wrong-type", {**source, "sourceType": "other"}),
        ("legacy", {"project_id": legacy_id, "project_root_hash": legacy_hash,
                    "agent_role": "analyst", "task_shape": "bugfix", "semantic_status": "active"}),
    ]:
        _capture(runner, f"shared checks {item_id}", item_id, **metadata)

    scope = {"project_id": legacy_id, "project_root_hash": legacy_hash,
             "agent_role": "analyst", "task_shape": "bugfix",
             "project_context": {"project_id": "project-a", "project_root_hash": source_hash}}
    request = _request(scope=scope, queries=["shared checks"], filters={"semantic_statuses": ["active"]})
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))

    assert result.exit_code == 0, result.output
    assert set(json.loads(result.output)["selected_ids"]) == {"common", "legacy"}

    request["scope"] = {
        "project_context": scope["project_context"],
        "agent_role": "analyst",
        "task_shape": "bugfix",
    }
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["selected_ids"] == ["common"]

    request["scope"] = {"agent_role": "analyst", "task_shape": "bugfix"}
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["selected_ids"] == ["legacy"]


def test_recall_korean_fallback_returns_only_local_active_source_candidates(runner: CliRunner) -> None:
    scope = {"project_id": "project-a", "project_root_hash": "0123456789abcdef"}
    source = {**scope, "sourceType": "project_context_section", "source_path": "decisions.md",
              "source_section": "질문 말풍선 개행", "semantic_status": "active"}
    for key, changes in (
        ("eligible", {}),
        ("wrong-project", {"project_id": "project-b"}),
        ("wrong-root", {"project_root_hash": "0123456789ffff00"}),
        ("inactive", {"semantic_status": "invalidated"}),
        ("missing-source", {"source_path": None}),
    ):
        _capture(runner, f"질문 말풍선 개행 원문 보존 {key}", key, **{**source, **changes})

    request = _request(
        queries=["질문 말풍선 위아래에 빈 줄이 붙을 때 원본 채팅도 정리해?"],
        scope={"project_context": scope}, filters={"layers": ["project"], "semantic_statuses": ["active"]},
    )
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["memory_id"] for row in payload["results"]] == ["eligible"]
    assert payload["selected_ids"] == []
    assert payload["results"][0]["source_section"] == "질문 말풍선 개행"
    assert payload["diagnostics"]["korean_candidate_count"] == 1

    request["scope"] = {}
    unscoped = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))

    assert unscoped.exit_code == 0, unscoped.output
    assert json.loads(unscoped.output)["results"] == []


def test_korean_candidate_expansion_keeps_normal_results_and_recovers_scoped_lesson(runner: CliRunner) -> None:
    scope = {"project_id": "project-a", "project_root_hash": "0123456789abcdef"}
    source = {"sourceType": "project_context_section", "source_section": "교훈", "semantic_status": "active"}
    _capture(runner, "export settings format and log retention", "primary", **scope, **source, source_path="primary.md")
    _capture(runner, "질문 말풍선 개행 원문 채팅 보존", "recovered", **scope, **source, source_path="recovered.md")
    _capture(runner, "질문 말풍선 개행 원문 포맷 유지", "wrong-root", project_id=scope["project_id"],
             project_root_hash="another-root", **source, source_path="other.md")
    request = _request(queries=["질문 말풍선 위아래에 빈 줄이 붙을 때 원본 채팅도 정리해?", "export settings"],
                       scope={"project_context": scope}, filters={"layers": ["project"], "semantic_statuses": ["active"]})

    def recall():
        result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    response = recall()
    assert [row["memory_id"] for row in response["results"]] == ["primary", "recovered"]
    assert response["selected_ids"] == ["primary"]
    assert response["diagnostics"]["korean_candidate_count"] == 1

    request["budget"]["candidate_limit"] = 1
    bounded = recall()
    assert [row["memory_id"] for row in bounded["results"]] == ["primary"]
    assert bounded["diagnostics"]["korean_candidate_count"] == 0


@pytest.mark.parametrize("noise_scope", ["other-checkout", "legacy-unscoped"])
def test_korean_candidates_prioritize_indexed_project_scope_before_global_limit(
    runner: CliRunner, repo_root: Path, noise_scope: str,
) -> None:
    from agents.source_adapters import ProjectContextIngestor, ProjectContextScanner, project_root_hash
    from core.gateway import MemoryGateway
    from core.korean import recall_candidate_query

    project = repo_root / "current" / "project-a"
    foreign = repo_root / "another" / "project-a"
    project.mkdir(parents=True)
    foreign.mkdir(parents=True)
    (project / "decisions.md").write_text("# 말풍선\n\n표시 문자열의 줄바꿈만 제거한다.\n", encoding="utf-8")
    for n in range(40):
        (foreign / f"parallel-{n}.md").write_text(
            "# 질문 말풍선\n\n질문 말풍선 원본 채팅 정리라는 단어를 쓰는 다른 체크아웃.\n", encoding="utf-8")

    gateway = MemoryGateway(repo_root=str(repo_root))
    ingestor = ProjectContextIngestor(gateway)
    local, = ingestor.ingest(ProjectContextScanner(project, project_id="project-a", project_root=project).discover()).created
    if noise_scope == "other-checkout":
        other_ids = ingestor.ingest(ProjectContextScanner(foreign, project_id="project-a", project_root=foreign).discover()).created
    else:
        other_ids = [gateway.capture(content=path.read_text(), layer="project", extra_metadata={"semantic_status": "active"},
                                     no_classify=True, skip_dedup=True) for path in foreign.glob("*.md")]
    query = "질문 말풍선 위아래에 빈 줄이 붙을 때 원본 채팅도 정리해?"
    global_candidates = gateway._fts.search(recall_candidate_query(query), limit=32)
    assert len(global_candidates) == 32 and all(row["item_id"] in other_ids for row in global_candidates)

    request = _request(queries=[query], scope={"project_context": {
        "project_id": "project-a", "project_root_hash": project_root_hash(project),
    }}, filters={"layers": ["project"], "semantic_statuses": ["active"]})
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["memory_id"] for row in payload["results"]] == [local]
    assert payload["selected_ids"] == []


def test_korean_candidate_order_uses_validated_feedback_before_the_result_limit(runner: CliRunner, repo_root: Path) -> None:
    from core.feedback import FeedbackStore

    scope = {"project_id": "project-a", "project_root_hash": "0123456789abcdef"}
    for name in ("first", "second"):
        _capture(runner, f"질문 말풍선 원문 보존 {name}", name, **scope,
                 sourceType="project_context_section", source_path=f"{name}.md",
                 source_section="말풍선 원문 보존", semantic_status="active")
    request = _request(queries=["질문 말풍선에 빈 줄이 붙으면 원본 입력도 정리해?"],
                       scope={"project_context": scope},
                       filters={"layers": ["project"], "semantic_statuses": ["active"]})

    def recall():
        result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
        assert result.exit_code == 0, result.output
        value = json.loads(result.output)
        assert value["selected_ids"] == []
        return [item["memory_id"] for item in value["results"]]

    before, preferred = recall()
    store = FeedbackStore(str(repo_root))

    def feedback(event: str, memory_id: str, task: str):
        store.record({"schema_version": "mnemos.feedback.request.v1", "event_id": f"{event}-{task}",
                      "event": event, "memory_id": memory_id, "task_id": task})

    for event in ("retrieved", "selected", "applied"):
        feedback(event, preferred, "unvalidated")
    assert recall() == [before, preferred]

    feedback("validated", preferred, "reviewed")
    request["budget"]["candidate_limit"] = 1

    assert recall() == [preferred]

    feedback("invalidated", preferred, "replaced")

    assert recall() == [before]


def test_recall_ranks_candidate_application_only_after_review(runner: CliRunner, repo_root: Path) -> None:
    from core.feedback import FeedbackStore

    scope = {"project_id": "project-a", "project_root_hash": "0123456789abcdef"}
    for name in ("first", "second"):
        _capture(runner, f"Check install link boundaries before writing files. {name}", name, **scope,
                 sourceType="project_context_section", source_path=f"{name}.md",
                 source_section="Install link boundary", semantic_status="active")
    request = _request(queries=["install link boundaries"], scope={"project_context": scope},
                       filters={"layers": ["project"], "semantic_statuses": ["active"]})

    def recall():
        result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["diagnostics"].get("korean_candidate_count", 0) == 0
        return payload["results"]

    before = recall()
    earlier, later = [row["memory_id"] for row in before]
    store = FeedbackStore(str(repo_root))

    def feedback(event: str, memory_id: str, task: str, reason_code: str):
        store.record({"schema_version": "mnemos.feedback.request.v1", "event_id": task,
                      "event": event, "memory_id": memory_id, "task_id": task, "reason_code": reason_code})

    feedback("applied", later, "candidate-use", "source_reviewed_candidate")

    observed = recall()
    assert [row["memory_id"] for row in observed] == [earlier, later]
    assert observed[1]["retrieval_score"] == before[1]["retrieval_score"]
    assert observed[1]["score_components"]["applied_count"] == 1

    feedback("validated", later, "candidate-use-review", "source_reviewed_candidate")

    assert [row["memory_id"] for row in recall()] == [later, earlier]

    feedback("applied", earlier, "normal-use", "source_verified")

    scores = {row["memory_id"]: row["score_components"]["validated_usage"] for row in recall()}
    assert scores[earlier] == pytest.approx(0.05)
    assert scores[later] == pytest.approx(0.35)

    legacy_projection = json.loads(store.projection_path.read_text(encoding="utf-8"))
    for usage in legacy_projection.values():
        usage.pop("candidate_applied_count")
    legacy_bytes = json.dumps(legacy_projection).encode("utf-8")
    store.projection_path.write_bytes(legacy_bytes)

    assert {row["memory_id"]: row["score_components"]["validated_usage"] for row in recall()} == scores
    assert store.projection_path.read_bytes() == legacy_bytes


def test_recall_rejects_incomplete_project_context_scope(runner: CliRunner) -> None:
    request = _request(scope={"project_context": {"project_id": "project-a"}})
    result = runner.invoke(cli, ["recall", "--json", "--request-file", "-"], input=json.dumps(request))

    assert result.exit_code != 0
    assert json.loads(result.output)["error"]["code"] == "validation_error"


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
