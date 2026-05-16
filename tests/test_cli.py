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

    def test_memory_capture_notification_short_content(self, runner, cli_with_repo):
        """capture notification must not add '...' when content <= 60 chars."""
        content = "Short insight"
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert content in result.output
        assert "..." not in result.output
        assert "(global)" in result.output

    def test_memory_capture_notification_long_content(self, runner, cli_with_repo):
        """capture notification must truncate content > 60 chars with '...'."""
        content = "A" * 80  # 80 chars, more than 60
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        preview = content[:60]
        assert f"{preview}..." in result.output
        assert "(global)" in result.output

    def test_memory_capture_notification_exactly_60_chars(self, runner, cli_with_repo):
        """capture notification must not add '...' when content is exactly 60 chars."""
        content = "B" * 60  # exactly 60 chars
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content, "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert content in result.output
        assert "..." not in result.output


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

    def test_memory_search_notification_with_results(self, runner, cli_with_repo):
        """search must print '[mnemos] Retrieved N memories' after results."""
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "notification test about dolphins"],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "dolphins"],
        )
        assert result.exit_code == 0, result.output
        assert "[mnemos] Retrieved" in result.output
        assert "memories" in result.output
        # Notification must appear at the end
        lines = result.output.strip().splitlines()
        assert lines[-1].startswith("[mnemos] Retrieved")

    def test_memory_search_notification_no_results(self, runner, cli_with_repo):
        """search must print '[mnemos] Retrieved 0 memories' when no results."""
        result = runner.invoke(
            cli_with_repo,
            ["search", "zzz-no-match-xyzzy-999"],
        )
        assert result.exit_code == 0, result.output
        assert "[mnemos] Retrieved 0 memories" in result.output

    def test_memory_search_notification_count_matches(self, runner, cli_with_repo):
        """search notification N must match the number of results returned."""
        content = "unique-kiwi-memory-for-count-test"
        runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", content],
        )
        result = runner.invoke(
            cli_with_repo,
            ["search", "kiwi-memory-for-count"],
        )
        assert result.exit_code == 0, result.output
        # Extract N from "[mnemos] Retrieved N memories"
        import re
        match = re.search(r"\[mnemos\] Retrieved (\d+) memories", result.output)
        assert match is not None, f"Notification not found in: {result.output}"
        n = int(match.group(1))
        # Count result lines (lines starting with "  [")
        result_lines = [l for l in result.output.splitlines() if l.startswith("  [")]
        assert n == len(result_lines), (
            f"Notification said {n} but found {len(result_lines)} result lines"
        )


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


class TestConsolidateCommand:
    """Tests for `mnemos consolidate` CLI command (Part 2 of issue #11)."""

    def test_consolidate_exits_zero(self, runner, cli_with_repo):
        """consolidate must exit 0 even on an empty store."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output

    def test_consolidate_output_format(self, runner, cli_with_repo):
        """consolidate must print 'Promoted N memories'."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Promoted" in result.output
        assert "memories" in result.output

    def test_consolidate_promotes_eligible_items(self, runner, cli_with_repo, repo_root):
        """consolidate must promote items that meet policy thresholds and report count."""
        import re
        # Capture items in project layer (zero thresholds in test policy → all eligible)
        for i in range(2):
            runner.invoke(
                cli_with_repo,
                [
                    "capture",
                    "--layer", "project",
                    "--content", f"Consolidate CLI test item {i}",
                    "--quality-score", "0.9",
                ],
            )

        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output

        match = re.search(r"Promoted (\d+) memories", result.output)
        assert match is not None, f"Expected 'Promoted N memories' in: {result.output}"
        n = int(match.group(1))
        assert n >= 2

        # Items must now be in global layer
        global_files = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(global_files) >= 2

    def test_consolidate_empty_store_reports_zero(self, runner, cli_with_repo):
        """consolidate on empty store must report 'Promoted 0 memories'."""
        result = runner.invoke(cli_with_repo, ["consolidate"])
        assert result.exit_code == 0, result.output
        assert "Promoted 0 memories" in result.output


class TestCaptureEphemeralDefault:
    """CLI tests for ephemeral-first capture default (issue #12)."""

    def test_capture_without_layer_defaults_to_ephemeral(self, runner, cli_with_repo, repo_root):
        """capture without --layer must write to .agent/runs/.../scratch/ and report ephemeral."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "Ephemeral CLI default test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "captured" in result.output.lower()
        assert "(ephemeral)" in result.output

        # File must exist somewhere under .agent/runs/{run_id}/scratch/
        agent_runs = repo_root / ".agent" / "runs"
        matches = list(agent_runs.rglob("*.md"))
        assert len(matches) >= 1, f"Expected ephemeral file under {agent_runs}"

    def test_capture_explicit_layer_still_works(self, runner, cli_with_repo, repo_root):
        """capture with --layer global must still write to wiki/global/."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Explicit global layer", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "(global)" in result.output
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1

    def test_capture_layer_is_optional_flag(self, runner, cli_with_repo):
        """capture must accept --content alone (--layer is now optional)."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "No layer flag provided"],
        )
        assert result.exit_code == 0, result.output


class TestCaptureANSIOutput:
    """Tests for colored italic ANSI output in capture command (issue #15)."""

    def test_capture_notice_contains_ansi_by_default(self, runner, cli_with_repo):
        """capture notice must include ANSI escape codes when --no-color is not set."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "ANSI styled output test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" in result.output

    def test_capture_notice_no_color_flag_strips_ansi(self, runner, cli_with_repo):
        """--no-color flag must produce plain text output without ANSI codes."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Plain text output test", "--no-color"],
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" not in result.output
        assert "(global)" in result.output

    def test_capture_notice_no_color_env_strips_ansi(self, runner, cli_with_repo):
        """NO_COLOR env var must produce plain text output without ANSI codes."""
        import os
        env = dict(os.environ)
        env["NO_COLOR"] = "1"
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "NO_COLOR env test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "\033[" not in result.output
        assert "(global)" in result.output

    def test_capture_notice_project_layer_uses_blue(self, runner, cli_with_repo):
        """project layer notice must use blue ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "project", "--content", "Project layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[94m" in result.output  # bright blue

    def test_capture_notice_session_layer_uses_gray(self, runner, cli_with_repo):
        """session layer notice must use gray ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "session", "--content", "Session layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[90m" in result.output  # gray

    def test_capture_notice_global_layer_uses_yellow(self, runner, cli_with_repo):
        """global layer notice must use yellow/gold ANSI color code."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Global layer color test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[33m" in result.output  # yellow/gold

    def test_capture_notice_includes_dim_and_italic(self, runner, cli_with_repo):
        """capture notice must include DIM and ITALIC ANSI codes."""
        import os
        env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "session", "--content", "Dim italic style test"],
            env=env,
            color=True,
        )
        assert result.exit_code == 0, result.output
        assert "\033[2m" in result.output  # dim
        assert "\033[3m" in result.output  # italic
        assert "\033[0m" in result.output  # reset

    def test_capture_notice_has_prefix_symbol(self, runner, cli_with_repo):
        """capture notice must include ✻ prefix symbol."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Prefix symbol test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "✻" in result.output
        assert "🧠" in result.output


class TestCaptureQuietFlag:
    """Tests for --quiet flag on the capture command."""

    def test_capture_quiet_suppresses_notice(self, runner, cli_with_repo):
        """--quiet must suppress the capture_notice line (no ✻ 🧠)."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Quiet flag test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" not in result.output
        assert "✻" not in result.output

    def test_capture_quiet_still_prints_id(self, runner, cli_with_repo):
        """--quiet must still output 'captured: <id>'."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Quiet ID output test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip().startswith("captured:")

    def test_capture_quiet_output_is_only_id_line(self, runner, cli_with_repo):
        """--quiet output must contain exactly one line: the captured ID."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Single line output test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("captured:")

    def test_capture_without_quiet_still_shows_notice(self, runner, cli_with_repo):
        """Without --quiet, capture_notice line must still be present."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--layer", "global", "--content", "Notice still shown test", "--no-color"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" in result.output
        assert "✻" in result.output

    def test_capture_quiet_with_ephemeral_layer(self, runner, cli_with_repo):
        """--quiet must suppress notice for ephemeral layer too."""
        result = runner.invoke(
            cli_with_repo,
            ["capture", "--content", "Ephemeral quiet test", "--quiet"],
        )
        assert result.exit_code == 0, result.output
        assert "🧠" not in result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        assert len(lines) == 1
        assert lines[0].startswith("captured:")
