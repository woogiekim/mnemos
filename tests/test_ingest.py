"""Tests for IngestAgent.run_scanner_results_dedup dry-run mode."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Minimal mnemos repo structure."""
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
# dry_run=True tests
# ---------------------------------------------------------------------------


class TestRunScannerResultsDedupDryRun:
    def test_dry_run_new_file_returns_file_path_not_id(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: new file → 'created' list contains file path string, not item ID."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# New content\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        result = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=True,
        )

        assert result["created"] == [str(md_file.resolve())]
        assert result["updated"] == []
        assert result["skipped"] == []

    def test_dry_run_new_file_does_not_write(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: capture() is never called for new files."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# New content\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        with patch.object(gw, "capture") as mock_capture:
            agent.run_scanner_results_dedup(
                [(md_file.resolve(), "global", "global")],
                run_id="test-run",
                dry_run=True,
            )
            mock_capture.assert_not_called()

    def test_dry_run_unchanged_file_returns_file_path_in_skipped(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: unchanged file → 'skipped' list contains file path string."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        content = "# Stable content\n"
        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text(content)

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        # First ingest (normal) so the item exists in the store
        agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=False,
        )

        # Second call with dry_run=True — content unchanged
        result = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=True,
        )

        assert result["skipped"] == [str(md_file.resolve())]
        assert result["created"] == []
        assert result["updated"] == []

    def test_dry_run_unchanged_file_does_not_write(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: no write calls even when existing item is skipped."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        content = "# Stable content\n"
        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text(content)

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        # First ingest (normal)
        agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=False,
        )

        with patch.object(gw, "capture") as mock_capture, patch.object(
            gw, "update"
        ) as mock_update:
            agent.run_scanner_results_dedup(
                [(md_file.resolve(), "global", "global")],
                run_id="test-run",
                dry_run=True,
            )
            mock_capture.assert_not_called()
            mock_update.assert_not_called()

    def test_dry_run_changed_file_returns_file_path_in_updated(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: changed file → 'updated' list contains file path string."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# Original content\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        # First ingest (normal)
        agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=False,
        )

        # Modify content
        md_file.write_text("# Changed content\n")

        # dry_run=True should detect the change
        result = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=True,
        )

        assert result["updated"] == [str(md_file.resolve())]
        assert result["created"] == []
        assert result["skipped"] == []

    def test_dry_run_changed_file_does_not_write(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=True: update() is never called for changed files."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# Original content\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        # First ingest (normal)
        agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
            dry_run=False,
        )

        # Modify content
        md_file.write_text("# Changed content\n")

        with patch.object(gw, "update") as mock_update:
            agent.run_scanner_results_dedup(
                [(md_file.resolve(), "global", "global")],
                run_id="test-run",
                dry_run=True,
            )
            mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# dry_run=False tests (normal behaviour unchanged)
# ---------------------------------------------------------------------------


class TestRunScannerResultsDedupNormal:
    def test_normal_new_file_returns_item_id(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=False (default): new file → 'created' list contains an item ID string."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# Content\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        result = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
        )

        assert len(result["created"]) == 1
        item_id = result["created"][0]
        # Item ID must not be a file path
        assert not item_id.startswith("/")
        assert result["updated"] == []
        assert result["skipped"] == []

    def test_normal_unchanged_file_skips(
        self, tmp_path: Path, repo_root: Path
    ) -> None:
        """dry_run=False: second ingest of unchanged file → skipped."""
        from core.gateway import MemoryGateway
        from agents.ingest import IngestAgent

        md_file = tmp_path / "CLAUDE.md"
        md_file.write_text("# Stable\n")

        gw = MemoryGateway(repo_root=str(repo_root))
        agent = IngestAgent(gateway=gw)

        first = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
        )
        second = agent.run_scanner_results_dedup(
            [(md_file.resolve(), "global", "global")],
            run_id="test-run",
        )

        assert first["created"] and second["skipped"]
        assert second["created"] == []
        assert second["updated"] == []
