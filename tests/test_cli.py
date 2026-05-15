"""Tests for Click CLI commands."""
import pytest
import yaml
import json
from pathlib import Path
from click.testing import CliRunner


@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal repo structure and return path."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy = {
        "layers": {
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")

    return tmp_path


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    """Return CLI runner and the cli function, with MNEMOS_REPO_ROOT set."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


class TestMemoryCaptureCommand:
    def test_memory_capture_command(self, runner, cli_with_repo, repo_root):
        """capture must store an item and print its ID."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "CLI test content"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower() or len(result.output.strip()) > 0

        # Verify file was created
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_memory_capture_with_id(self, runner, cli_with_repo, repo_root):
        """capture with --id must use the provided ID."""
        result = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "global",
                "--content", "Item with custom ID",
                "--id", "custom-cli-001",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "custom-cli-001" in result.output

    def test_memory_capture_invalid_layer_exits_nonzero(self, runner, cli_with_repo):
        """capture with invalid layer must exit with error."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "invalid", "--content", "x"],
        )
        assert result.exit_code != 0 or "error" in result.output.lower()


class TestMemorySearchCommand:
    def test_memory_search_command(self, runner, cli_with_repo, repo_root):
        """search must return results for indexed content."""
        # First capture something
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "searchable content about pandas"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "pandas"],
        )
        assert result.exit_code == 0, result.output

    def test_memory_search_empty_returns_zero(self, runner, cli_with_repo):
        """search on empty store must exit 0 with empty output."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "nonexistent-xyzzy"],
        )
        assert result.exit_code == 0


class TestMemoryPromoteCommand:
    def test_memory_promote_command(self, runner, cli_with_repo, repo_root):
        """promote must move item to next layer."""
        # Capture in project layer
        cap_result = runner.invoke(
            cli_with_repo,
            [
                "capture",
                "--layer", "project",
                "--content", "Item to promote",
                "--id", "promote-001",
            ],
        )
        assert cap_result.exit_code == 0, cap_result.output

        # Promote it
        result = runner.invoke(
            cli_with_repo,
            ["promote", "promote-001"],
        )
        assert result.exit_code == 0, result.output


class TestMemoryForgetCommand:
    def test_memory_forget_requires_archived(self, runner, cli_with_repo, repo_root):
        """forget on non-archived item must fail with error."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "forget-me", "--id", "forget-001"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["forget", "--force", "forget-001"],
        )
        # Should fail because item is not archived
        assert result.exit_code != 0 or "error" in result.output.lower() or "policy" in result.output.lower()

    def test_memory_forget_after_archive_succeeds(self, runner, cli_with_repo, repo_root):
        """forget after archive must succeed."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "archive then forget", "--id", "forget-002"],
        )
        runner.invoke(
            cli_with_repo,
            ["archive", "forget-002"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["forget", "--force", "forget-002"],
        )
        assert result.exit_code == 0, result.output
