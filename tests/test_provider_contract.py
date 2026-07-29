"""Tests for the stable mnemos provider contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli


@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal mnemos repo for CLI provider tests."""
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for dirname in ["runs", "sessions", "state", "reports", "tools", "transient"]:
        (agent / dirname).mkdir(parents=True)

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
def runner():
    """Return a Click test runner."""
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    """Return the CLI with MNEMOS_REPO_ROOT pointing at the temp repo."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "none")
    return cli


class TestProviderCapabilities:
    def test_capabilities_json_shape(self) -> None:
        """capabilities --json exposes stable compatibility metadata."""
        result = CliRunner().invoke(cli, ["capabilities", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["provider"] == "mnemos"
        assert payload["provider_contract_version"]
        assert payload["capabilities"]["capture_json"] is True
        assert payload["capabilities"]["fast_search"] is True
        assert payload["capabilities"]["search_scores"] is True
        assert payload["capabilities"]["search_read_only_default"] == "supported"
        assert payload["capabilities"]["search_touch_legacy"] == "deprecated"
        assert payload["capability_status"]["retrieval_backend_health"] == "supported"
        assert payload["capability_status"]["retrieval_degradation_evidence"] == "supported"
        assert payload["capability_status"]["memory_os_readiness_audit"] == "supported"
        assert payload["status_values"] == ["supported", "unsupported", "deprecated", "unknown"]
        assert payload["capability_status"]["capture_json"] == "supported"
        assert payload["capability_status"]["read_json"] == "supported"
        assert payload["capability_descriptions"]["fast_search"]
        assert "mnemos search --touch" in payload["capability_descriptions"]["search_touch_legacy"]

    def test_search_recall_feedback_transition_doc_exists(self) -> None:
        doc = Path("docs/search-recall-feedback.md")

        assert doc.exists()
        content = doc.read_text(encoding="utf-8")
        assert "mnemos search --touch" in content
        assert "mnemos recall" in content
        assert "mnemos feedback" in content

    def test_provider_payload_marks_access_count_as_legacy(self) -> None:
        from core.provider import memory_item_payload, search_result_payload

        item = memory_item_payload({"id": "m1", "content": "x", "access_count": 3})
        result = search_result_payload({"item_id": "m1", "content": "x", "metadata": {"access_count": 4}})

        assert item["metadata"]["legacy_access_count"] == 3
        assert result["metadata"]["legacy_access_count"] == 4

    def test_capability_status_covers_backward_compatible_boolean_keys(self) -> None:
        """Every boolean capability has a tri-state status for new integrations."""
        result = CliRunner().invoke(cli, ["capabilities", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        expected = {
            "capture_json",
            "search_json",
            "fast_search",
            "search_scores",
            "read_json",
            "gc_json",
            "host_install",
        }
        assert expected.issubset(payload["capabilities"])
        assert expected.issubset(payload["capability_status"])
        assert set(payload["capability_status"].values()).issubset(payload["status_values"])
        assert all(isinstance(payload["capabilities"][name], bool) for name in expected)

    def test_version_json_includes_capabilities(self) -> None:
        """version --json lets integrations inspect version and features together."""
        result = CliRunner().invoke(cli, ["version", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["provider"] == "mnemos"
        assert "version" in payload
        assert payload["capabilities"]["read_json"] is True
        assert payload["capability_status"]["read_json"] == "supported"
        assert payload["status_values"] == ["supported", "unsupported", "deprecated", "unknown"]


class TestProviderCommandJson:
    def test_search_json_no_results_is_stable(self, runner, cli_with_repo) -> None:
        """No-result searches return an empty result set, not text-only output."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "--fast", "--json", "no such provider memory"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["count"] == 0
        assert payload["partial_failure"] is False
        assert payload["retrieval_diagnostics"]["status"] == "ok"
        assert payload["results"] == []

    def test_capture_search_read_json_contract(self, runner, cli_with_repo) -> None:
        """Host integrations can capture, search, and read without parsing text output."""
        capture = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "global",
                "--content", "Provider contract searchable item",
                "--id", "provider-json-001",
                "--json",
                "--no-classify",
            ],
        )
        assert capture.exit_code == 0, capture.output
        capture_payload = json.loads(capture.output)
        assert capture_payload["status"] == "captured"
        assert capture_payload["id"] == "provider-json-001"

        search = runner.invoke(
            cli_with_repo,
            ["search", "--fast", "--json", "searchable"],
        )
        assert search.exit_code == 0, search.output
        search_payload = json.loads(search.output)
        assert search_payload["mode"] == "fast"
        assert search_payload["partial_failure"] is False
        assert search_payload["score_scale"]["min"] == 0.0
        assert search_payload["score_scale"]["max"] == 1.0
        assert search_payload["score_scale"]["direction"] == "higher_is_more_relevant"
        assert search_payload["results"][0]["id"] == "provider-json-001"
        assert search_payload["results"][0]["score"] == 1.0

        read = runner.invoke(
            cli_with_repo,
            ["read", "--json", "provider-json-001"],
        )
        assert read.exit_code == 0, read.output
        read_payload = json.loads(read.output)
        assert read_payload["id"] == "provider-json-001"
        assert read_payload["layer"] == "global"

    def test_fast_search_json_omits_backend_rank(self, runner, cli_with_repo) -> None:
        """Fast search JSON exposes stable relevance scores, not backend rank values."""
        for item_id, content in [
            ("score-json-001", "stable fast search alpha alpha"),
            ("score-json-002", "stable fast search alpha"),
        ]:
            capture = runner.invoke(
                cli_with_repo,
                [
                    "capture",
                    "--layer", "global",
                    "--content", content,
                    "--id", item_id,
                    "--json",
                    "--no-classify",
                ],
            )
            assert capture.exit_code == 0, capture.output

        result = runner.invoke(
            cli_with_repo,
            ["search", "--fast", "--json", "--limit", "2", "alpha"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        scores = [item["score"] for item in payload["results"]]
        assert payload["count"] == 2
        assert all(0.0 <= score <= 1.0 for score in scores)
        assert scores == sorted(scores, reverse=True)
        assert all("rank" not in item for item in payload["results"])

    def test_fast_search_json_empty_result_shape(self, runner, cli_with_repo) -> None:
        """No-result fast search returns count zero and an empty results list."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "--fast", "--json", "--limit", "5", "missing-term-xyz"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 0
        assert payload["results"] == []
        assert payload["score_scale"]["semantics"]

    def test_gc_json_contract(self, runner, cli_with_repo) -> None:
        """gc --json returns structured status even when no items are archived."""
        result = runner.invoke(cli_with_repo, ["gc", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "dry_run"
        assert "archived_count" in payload
        assert "regions_processed" in payload

    def test_capture_json_policy_error_is_structured(self, runner, cli_with_repo) -> None:
        """Capture JSON errors use the provider envelope."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--json", "--layer", "invalid-layer", "--content", "x"],
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "policy_violation"
        assert payload["error"]["retryable"] is False

    def test_read_json_missing_item_returns_structured_error(self, runner, cli_with_repo) -> None:
        """Host integrations can handle missing reads without parsing stderr."""
        result = runner.invoke(cli_with_repo, ["read", "--json", "missing-provider-id"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "not_found"
        assert payload["error"]["retryable"] is False

    def test_search_json_backend_failure_returns_partial_failure(
        self,
        runner,
        cli_with_repo,
        monkeypatch,
    ) -> None:
        """Search backend failures degrade to structured JSON for timeout-safe hosts."""
        from core import cli as cli_module

        class BrokenGateway:
            def search(self, **_kwargs):
                raise TimeoutError("index search timed out")

        monkeypatch.setattr(cli_module, "_get_gateway", lambda: BrokenGateway())

        result = runner.invoke(cli_with_repo, ["search", "--fast", "--json", "anything"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "degraded"
        assert payload["partial_failure"] is True
        assert payload["results"] == []
        assert payload["error"]["code"] == "timeout"
        assert payload["error"]["retryable"] is True

    def test_search_json_locked_backend_is_retryable(
        self,
        runner,
        cli_with_repo,
        monkeypatch,
    ) -> None:
        """Locked vault or index errors are explicit retryable provider failures."""
        from core import cli as cli_module

        class LockedGateway:
            def search(self, **_kwargs):
                raise RuntimeError("database is locked")

        monkeypatch.setattr(cli_module, "_get_gateway", lambda: LockedGateway())

        result = runner.invoke(cli_with_repo, ["search", "--json", "anything"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "degraded"
        assert payload["partial_failure"] is True
        assert payload["error"]["code"] == "locked"
        assert payload["error"]["retryable"] is True

    def test_search_json_exposes_vector_degradation(
        self,
        runner,
        cli_with_repo,
        monkeypatch,
    ) -> None:
        """Search JSON reports configured vector backend degradation without failing retrieval."""
        monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "invalid_backend")

        result = runner.invoke(cli_with_repo, ["search", "--fast", "--json", "anything"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        diagnostics = payload["retrieval_diagnostics"]
        vector = next(backend for backend in diagnostics["backends"] if backend["name"] == "vector")
        assert payload["status"] == "degraded"
        assert payload["partial_failure"] is True
        assert vector["status"] == "unsupported"
        assert vector["degraded"] is True
