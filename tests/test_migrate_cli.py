"""Tests for `mnemos migrate` CLI command.

Tests: forward migration, reverse migration, dry-run, idempotency.
TDD: written before the implementation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter
import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_policy(tmp_path: Path) -> dict[str, Any]:
    return {
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
        "archive": {"allowed_stages": ["stored"]},
    }


@pytest.fixture
def default_repo(tmp_path):
    """Create a minimal default-backend repo with some items."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)
    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (wiki / "policy.yaml").write_text(yaml.dump(_make_policy(tmp_path)))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")

    # Seed some project items in the default store
    from core.store import MemoryStore
    store = MemoryStore(repo_root=str(tmp_path))
    store.write(
        layer="project",
        item_id="migrate-proj-001",
        content="project memory one",
        metadata={"id": "migrate-proj-001", "layer": "project",
                  "quality_score": 0.8, "tags": ["test"], "access_count": 0},
    )
    store.write(
        layer="global",
        item_id="migrate-glo-001",
        content="global memory one",
        metadata={"id": "migrate-glo-001", "layer": "global",
                  "quality_score": 0.9, "tags": [], "access_count": 2},
    )
    return tmp_path


@pytest.fixture
def vault_dir(tmp_path):
    """Return a fresh vault directory path."""
    v = tmp_path / "obsidian_vault"
    v.mkdir()
    return v


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Forward migration: default → obsidian
# ---------------------------------------------------------------------------


class TestForwardMigration:
    def _find_vault_item(self, vault_dir: Path, item_id: str) -> "Path | None":
        """Scan vault layers and return the path of the file with matching id frontmatter."""
        for layer_dir in vault_dir.iterdir():
            if not layer_dir.is_dir():
                continue
            for md_file in layer_dir.glob("*.md"):
                try:
                    post = frontmatter.load(str(md_file))
                    if post.metadata.get("id") == item_id:
                        return md_file
                except Exception:
                    pass
        return None

    def test_migrate_default_to_obsidian_creates_vault_files(
        self, default_repo, vault_dir, runner
    ):
        """Items from the default store appear as .md files in the vault."""
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        assert result.exit_code == 0, f"CLI exited with {result.exit_code}: {result.output}"
        # Files use slug-based names; find them by frontmatter id
        assert self._find_vault_item(vault_dir, "migrate-proj-001") is not None
        assert self._find_vault_item(vault_dir, "migrate-glo-001") is not None

    def test_migrate_preserves_content(self, default_repo, vault_dir, runner):
        """Migrated items preserve their original content."""
        from core.cli import cli
        runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        path = self._find_vault_item(vault_dir, "migrate-proj-001")
        assert path is not None
        post = frontmatter.load(str(path))
        assert post.content == "project memory one"
        assert post.metadata["id"] == "migrate-proj-001"

    def test_migrate_preserves_metadata(self, default_repo, vault_dir, runner):
        """Migrated items preserve quality_score and tags."""
        from core.cli import cli
        runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        path = self._find_vault_item(vault_dir, "migrate-proj-001")
        assert path is not None
        post = frontmatter.load(str(path))
        assert post.metadata["quality_score"] == 0.8
        assert post.metadata["tags"] == ["test"]

    def test_migrate_output_reports_count(self, default_repo, vault_dir, runner):
        """CLI output includes item count."""
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        # Should mention how many items were migrated
        assert "migrated" in result.output.lower() or "2" in result.output


# ---------------------------------------------------------------------------
# Reverse migration: obsidian → default
# ---------------------------------------------------------------------------


class TestReverseMigration:
    def _seed_vault(self, vault_dir: Path) -> None:
        """Seed the vault with items for reverse migration."""
        for layer in ("project", "global"):
            (vault_dir / layer).mkdir(parents=True, exist_ok=True)

        proj_post = frontmatter.Post(
            "vault project content",
            id="vault-proj-001",
            layer="project",
            quality_score=0.8,
            tags=["vault"],
            access_count=0,
            content_hash="abc",
        )
        with (vault_dir / "project" / "vault-proj-001.md").open("w") as f:
            f.write(frontmatter.dumps(proj_post))

        glo_post = frontmatter.Post(
            "vault global content",
            id="vault-glo-001",
            layer="global",
            quality_score=0.7,
            tags=[],
            access_count=1,
            content_hash="def",
        )
        with (vault_dir / "global" / "vault-glo-001.md").open("w") as f:
            f.write(frontmatter.dumps(glo_post))

    def test_migrate_obsidian_to_default_creates_store_files(
        self, default_repo, vault_dir, runner
    ):
        """Items from the vault appear in the default MemoryStore."""
        self._seed_vault(vault_dir)
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--from", "obsidian", "--to", "default",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        assert result.exit_code == 0, f"CLI error: {result.output}"
        # Items should now exist in the default store
        from core.store import MemoryStore
        store = MemoryStore(repo_root=str(default_repo))
        item = store.read("vault-proj-001")
        assert item["content"] == "vault project content"

    def test_migrate_reverse_preserves_layer(
        self, default_repo, vault_dir, runner
    ):
        """Reverse migration places items in correct layer directories."""
        self._seed_vault(vault_dir)
        from core.cli import cli
        runner.invoke(
            cli,
            ["migrate", "--from", "obsidian", "--to", "default",
             "--vault-path", str(vault_dir)],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        # global item should be in wiki/global/
        assert (default_repo / "wiki" / "global" / "vault-glo-001.md").exists()


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_does_not_write_files(self, default_repo, vault_dir, runner):
        """--dry-run prints plan but does not create vault files."""
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir), "--dry-run"],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        assert result.exit_code == 0
        # No .md files should have been created in any layer directory
        for layer_dir in vault_dir.iterdir():
            if layer_dir.is_dir():
                assert list(layer_dir.glob("*.md")) == [], (
                    f"Dry-run must not create files in {layer_dir}"
                )

    def test_dry_run_output_describes_plan(self, default_repo, vault_dir, runner):
        """--dry-run output mentions the items that would be migrated."""
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--from", "default", "--to", "obsidian",
             "--vault-path", str(vault_dir), "--dry-run"],
            env={"MNEMOS_REPO_ROOT": str(default_repo)},
        )
        output = result.output.lower()
        assert "dry" in output or "would" in output or "migrate-proj-001" in result.output


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def _find_vault_file(self, vault_dir: Path, item_id: str) -> "Path | None":
        for layer_dir in vault_dir.iterdir():
            if not layer_dir.is_dir():
                continue
            for md_file in layer_dir.glob("*.md"):
                try:
                    post = frontmatter.load(str(md_file))
                    if post.metadata.get("id") == item_id:
                        return md_file
                except Exception:
                    pass
        return None

    def test_re_run_skips_already_migrated_items(self, default_repo, vault_dir, runner):
        """Re-running migrate skips items whose (id, content_hash) already match."""
        from core.cli import cli
        args = ["migrate", "--from", "default", "--to", "obsidian",
                "--vault-path", str(vault_dir)]
        env = {"MNEMOS_REPO_ROOT": str(default_repo)}

        # First run
        runner.invoke(cli, args, env=env)
        # Find the actual slug-based path for the migrated item
        proj_file = self._find_vault_file(vault_dir, "migrate-proj-001")
        assert proj_file is not None, "First migration should have created a vault file"
        mtime_after_first = proj_file.stat().st_mtime

        import time
        time.sleep(0.05)  # ensure mtime resolution

        # Second run
        result = runner.invoke(cli, args, env=env)
        assert result.exit_code == 0
        mtime_after_second = proj_file.stat().st_mtime

        # File must NOT have been rewritten on second run
        assert mtime_after_first == mtime_after_second, (
            "Idempotent migrate must not overwrite files with matching (id, content_hash)"
        )

    def test_re_run_output_reports_skipped(self, default_repo, vault_dir, runner):
        """Second run should mention that items were skipped."""
        from core.cli import cli
        args = ["migrate", "--from", "default", "--to", "obsidian",
                "--vault-path", str(vault_dir)]
        env = {"MNEMOS_REPO_ROOT": str(default_repo)}
        runner.invoke(cli, args, env=env)
        result = runner.invoke(cli, args, env=env)
        output = result.output.lower()
        assert "skip" in output or "already" in output or "0 migrated" in output
