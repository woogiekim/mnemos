"""Tests for ClaudeMdScanner and memory-ingest-claude-md CLI command."""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Minimal mnemos repo structure for gateway/CLI tests."""
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


# ---------------------------------------------------------------------------
# ClaudeMdScanner unit tests
# ---------------------------------------------------------------------------


class TestClaudeMdScannerDiscover:
    def test_discovers_both_files_when_present(self, tmp_path: Path) -> None:
        """discover() returns two entries when both global and project CLAUDE.md exist."""
        from agents.scanner import ClaudeMdScanner

        global_dir = tmp_path / ".claude"
        global_dir.mkdir(parents=True)
        global_md = global_dir / "CLAUDE.md"
        global_md.write_text("# Global instructions\n")

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_md = project_dir / "CLAUDE.md"
        project_md.write_text("# Project instructions\n")

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", global_md):
            scanner = ClaudeMdScanner(project_root=project_dir)
            results = scanner.discover()

        assert len(results) == 2
        paths = [r[0] for r in results]
        layers = [r[1] for r in results]
        scopes = [r[2] for r in results]

        assert global_md.resolve() in paths
        assert (project_dir / "CLAUDE.md").resolve() in paths
        assert "global" in layers
        assert "project" in layers
        assert "global" in scopes
        assert "project" in scopes

    def test_skips_missing_global_file(self, tmp_path: Path) -> None:
        """discover() skips ~/.claude/CLAUDE.md silently when it does not exist."""
        from agents.scanner import ClaudeMdScanner

        missing_global = tmp_path / "nonexistent" / "CLAUDE.md"

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Project only\n")

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", missing_global):
            scanner = ClaudeMdScanner(project_root=project_dir)
            results = scanner.discover()

        assert len(results) == 1
        assert results[0][1] == "project"
        assert results[0][2] == "project"

    def test_skips_missing_project_file(self, tmp_path: Path) -> None:
        """discover() skips project CLAUDE.md silently when it does not exist."""
        from agents.scanner import ClaudeMdScanner

        global_dir = tmp_path / ".claude"
        global_dir.mkdir(parents=True)
        global_md = global_dir / "CLAUDE.md"
        global_md.write_text("# Global only\n")

        project_dir = tmp_path / "no-claude-md-here"
        project_dir.mkdir()

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", global_md):
            scanner = ClaudeMdScanner(project_root=project_dir)
            results = scanner.discover()

        assert len(results) == 1
        assert results[0][1] == "global"
        assert results[0][2] == "global"

    def test_returns_empty_when_no_files_exist(self, tmp_path: Path) -> None:
        """discover() returns an empty list when neither CLAUDE.md exists."""
        from agents.scanner import ClaudeMdScanner

        missing_global = tmp_path / ".claude" / "CLAUDE.md"
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", missing_global):
            scanner = ClaudeMdScanner(project_root=project_dir)
            results = scanner.discover()

        assert results == []

    def test_default_project_root_is_cwd(self, tmp_path: Path) -> None:
        """ClaudeMdScanner with no project_root defaults to cwd."""
        from agents.scanner import ClaudeMdScanner

        with patch("agents.scanner.Path.cwd", return_value=tmp_path):
            scanner = ClaudeMdScanner()
        # project CLAUDE.md candidate should be tmp_path / "CLAUDE.md"
        assert scanner._project_root == tmp_path.resolve()


# ---------------------------------------------------------------------------
# IngestAgent.run_scanner_results unit tests
# ---------------------------------------------------------------------------


class TestIngestAgentScannerResults:
    def test_ingests_global_file_to_global_layer(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """run_scanner_results routes global scope to layer 'global'."""
        import frontmatter
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        global_md = tmp_path / "global_CLAUDE.md"
        global_md.write_text("# Global agent instructions\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        scan_results = [(global_md.resolve(), "global", "global")]
        ids = agent.run_scanner_results(scan_results, run_id="test-run")

        assert len(ids) == 1
        # Verify file landed in wiki/global/
        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert len(matches) >= 1
        post = frontmatter.load(str(matches[0]))
        assert post.get("sourceType") == "claude_md"
        assert post.get("source_scope") == "global"
        assert "claude_md" in post.get("tags", [])
        assert "ingest" in post.get("tags", [])

    def test_ingests_project_file_to_project_layer(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """run_scanner_results routes project scope to layer 'project'."""
        import frontmatter
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        project_md = tmp_path / "project_CLAUDE.md"
        project_md.write_text("# Project agent instructions\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        scan_results = [(project_md.resolve(), "project", "project")]
        ids = agent.run_scanner_results(scan_results, run_id="test-run")

        assert len(ids) == 1
        matches = list((repo_root / "wiki" / "projects").glob("*.md"))
        assert len(matches) >= 1
        post = frontmatter.load(str(matches[0]))
        assert post.get("sourceType") == "claude_md"
        assert post.get("source_scope") == "project"

    def test_skips_missing_file_silently(self, repo_root: Path) -> None:
        """run_scanner_results skips a non-existent path without raising."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        missing = Path("/nonexistent/path/CLAUDE.md")
        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        ids = agent.run_scanner_results([(missing, "global", "global")], run_id="test-run")
        assert ids == []

    def test_metadata_source_file_is_absolute_string(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """source_file in extra_metadata must be an absolute path string."""
        import frontmatter
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# Test\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        ids = agent.run_scanner_results(
            [(md_file.resolve(), "global", "global")], run_id="test-run"
        )
        assert ids

        matches = list((repo_root / "wiki" / "global").glob("*.md"))
        assert matches
        post = frontmatter.load(str(matches[0]))
        source_file = post.get("source_file", "")
        assert Path(source_file).is_absolute()


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestMemoryIngestClaudeMdCommand:
    def _make_cli(self, monkeypatch, repo_root: Path):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        from core.cli import cli
        return cli

    def test_ingests_global_and_project_files(
        self, tmp_path: Path, repo_root: Path, runner: CliRunner, monkeypatch
    ) -> None:
        """memory-ingest-claude-md ingests both files and prints captured IDs."""
        from agents.scanner import ClaudeMdScanner

        global_dir = tmp_path / ".claude"
        global_dir.mkdir(parents=True)
        global_md = global_dir / "CLAUDE.md"
        global_md.write_text("# Global\n")

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Project\n")

        cli = self._make_cli(monkeypatch, repo_root)

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", global_md):
            result = runner.invoke(
                cli,
                ["memory-ingest-claude-md", "--project-root", str(project_dir)],
            )

        assert result.exit_code == 0, result.output
        assert "ingested:" in result.output
        assert "done: 2 item(s) ingested" in result.output

    def test_skips_missing_files_gracefully(
        self, tmp_path: Path, repo_root: Path, runner: CliRunner, monkeypatch
    ) -> None:
        """CLI command handles no CLAUDE.md files without error."""
        from agents.scanner import ClaudeMdScanner

        missing_global = tmp_path / ".claude" / "CLAUDE.md"
        empty_project = tmp_path / "empty-project"
        empty_project.mkdir()

        cli = self._make_cli(monkeypatch, repo_root)

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", missing_global):
            result = runner.invoke(
                cli,
                ["memory-ingest-claude-md", "--project-root", str(empty_project)],
            )

        assert result.exit_code == 0, result.output
        assert "no CLAUDE.md files found" in result.output

    def test_ingests_only_project_file_when_global_missing(
        self, tmp_path: Path, repo_root: Path, runner: CliRunner, monkeypatch
    ) -> None:
        """CLI ingests only the project file when global CLAUDE.md is absent."""
        from agents.scanner import ClaudeMdScanner

        missing_global = tmp_path / ".claude" / "CLAUDE.md"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Project only\n")

        cli = self._make_cli(monkeypatch, repo_root)

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", missing_global):
            result = runner.invoke(
                cli,
                ["memory-ingest-claude-md", "--project-root", str(project_dir)],
            )

        assert result.exit_code == 0, result.output
        assert "done: 1 item(s) ingested" in result.output

    def test_custom_run_id_is_accepted(
        self, tmp_path: Path, repo_root: Path, runner: CliRunner, monkeypatch
    ) -> None:
        """--run-id flag is accepted and does not cause an error."""
        from agents.scanner import ClaudeMdScanner

        missing_global = tmp_path / ".claude" / "CLAUDE.md"
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "CLAUDE.md").write_text("# Run-id test\n")

        cli = self._make_cli(monkeypatch, repo_root)

        with patch.object(ClaudeMdScanner, "GLOBAL_CLAUDE_MD", missing_global):
            result = runner.invoke(
                cli,
                [
                    "memory-ingest-claude-md",
                    "--project-root", str(project_dir),
                    "--run-id", "custom-run-123",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "done: 1 item(s) ingested" in result.output
