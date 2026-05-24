"""Tests for the stable mnemos provider contract."""
from __future__ import annotations

import json

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
        assert payload["status_values"] == ["supported", "unsupported", "unknown"]

    def test_version_json_includes_capabilities(self) -> None:
        """version --json lets integrations inspect version and features together."""
        result = CliRunner().invoke(cli, ["version", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["provider"] == "mnemos"
        assert "version" in payload
        assert payload["capabilities"]["read_json"] is True


class TestProviderCommandJson:
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
        assert search_payload["results"][0]["id"] == "provider-json-001"
        assert "score" in search_payload["results"][0]

        read = runner.invoke(
            cli_with_repo,
            ["read", "--json", "provider-json-001"],
        )
        assert read.exit_code == 0, read.output
        read_payload = json.loads(read.output)
        assert read_payload["id"] == "provider-json-001"
        assert read_payload["layer"] == "global"

    def test_gc_json_contract(self, runner, cli_with_repo) -> None:
        """gc --json returns structured status even when no items are archived."""
        result = runner.invoke(cli_with_repo, ["gc", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "dry_run"
        assert "archived_count" in payload
        assert "regions_processed" in payload
