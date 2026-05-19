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


# ---------------------------------------------------------------------------
# UUID-to-slug migration
# ---------------------------------------------------------------------------


class TestUuidToSlugMigration:
    """Tests for `mnemos migrate --uuid-to-slug`."""

    # UUID pattern used throughout
    _UUID1 = "fd80f3fd-a0ad-4540-9cb1-f09423641988"
    _UUID2 = "e9192884-0aa9-4ab6-8588-3899da15c506"

    def _seed_uuid_vault(self, vault_dir: Path) -> None:
        """Seed vault with UUID-named files (session layer)."""
        session_dir = vault_dir / "session"
        session_dir.mkdir(parents=True, exist_ok=True)

        post1 = frontmatter.Post(
            "hello world content",
            id=self._UUID1,
            layer="session",
            quality_score=0.8,
            tags=[],
            access_count=0,
            content_hash="abc",
        )
        (session_dir / f"{self._UUID1}.md").write_text(frontmatter.dumps(post1))

        post2 = frontmatter.Post(
            "slug test direct content",
            id=self._UUID2,
            layer="session",
            quality_score=0.7,
            tags=["test"],
            access_count=1,
            content_hash="def",
        )
        (session_dir / f"{self._UUID2}.md").write_text(frontmatter.dumps(post2))

    def _seed_mixed_vault(self, vault_dir: Path) -> None:
        """Seed vault with both UUID-named and already-slug-named files."""
        session_dir = vault_dir / "session"
        session_dir.mkdir(parents=True, exist_ok=True)

        # UUID-named file
        post_uuid = frontmatter.Post(
            "uuid file content",
            id=self._UUID1,
            layer="session",
            quality_score=0.8,
            tags=[],
            access_count=0,
            content_hash="abc",
        )
        (session_dir / f"{self._UUID1}.md").write_text(frontmatter.dumps(post_uuid))

        # Already slug-named file
        post_slug = frontmatter.Post(
            "already slugged content",
            id="already-slug-id",
            layer="session",
            quality_score=0.8,
            tags=[],
            access_count=0,
            content_hash="xyz",
        )
        (session_dir / "already-slugged-content.md").write_text(frontmatter.dumps(post_slug))

    def test_uuid_files_renamed_to_slug(self, vault_dir, runner):
        """UUID-named files should be renamed to slug-based names."""
        self._seed_uuid_vault(vault_dir)
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)],
        )
        assert result.exit_code == 0, f"CLI error: {result.output}"
        session_dir = vault_dir / "session"
        # UUID-named files should no longer exist
        assert not (session_dir / f"{self._UUID1}.md").exists(), "UUID file should have been renamed"
        assert not (session_dir / f"{self._UUID2}.md").exists(), "UUID file should have been renamed"
        # At least one slug file should exist
        md_files = list(session_dir.glob("*.md"))
        assert len(md_files) == 2, f"Expected 2 files, got {len(md_files)}"

    def test_non_uuid_files_skipped(self, vault_dir, runner):
        """Already-slug-named files must not be touched."""
        self._seed_mixed_vault(vault_dir)
        from core.cli import cli
        slug_file = vault_dir / "session" / "already-slugged-content.md"
        mtime_before = slug_file.stat().st_mtime

        result = runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)],
        )
        assert result.exit_code == 0, f"CLI error: {result.output}"
        assert slug_file.exists(), "Slug-named file should not have been removed"
        # mtime should not have changed (file not touched)
        assert slug_file.stat().st_mtime == mtime_before, "Slug file should not be rewritten"

    def test_frontmatter_id_preserved_after_rename(self, vault_dir, runner):
        """After rename, the `id` field in frontmatter must still be the UUID."""
        self._seed_uuid_vault(vault_dir)
        from core.cli import cli
        runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)],
        )
        session_dir = vault_dir / "session"
        # Find the renamed file for UUID1
        found_id = None
        for md_file in session_dir.glob("*.md"):
            post = frontmatter.load(str(md_file))
            if post.metadata.get("id") == self._UUID1:
                found_id = post.metadata["id"]
                break
        assert found_id == self._UUID1, "UUID must remain in frontmatter id field"

    def test_content_preserved_after_rename(self, vault_dir, runner):
        """After rename, the file content must be unchanged."""
        self._seed_uuid_vault(vault_dir)
        from core.cli import cli
        runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)],
        )
        session_dir = vault_dir / "session"
        # Find the renamed file for UUID1 by its id in frontmatter
        found_content = None
        for md_file in session_dir.glob("*.md"):
            post = frontmatter.load(str(md_file))
            if post.metadata.get("id") == self._UUID1:
                found_content = post.content
                break
        assert found_content == "hello world content", f"Content changed: {found_content!r}"

    def test_dry_run_does_not_rename(self, vault_dir, runner):
        """--dry-run must not rename any files."""
        self._seed_uuid_vault(vault_dir)
        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir), "--dry-run"],
        )
        assert result.exit_code == 0, f"CLI error: {result.output}"
        session_dir = vault_dir / "session"
        # UUID files should still be present
        assert (session_dir / f"{self._UUID1}.md").exists(), "Dry-run must not rename files"
        assert (session_dir / f"{self._UUID2}.md").exists(), "Dry-run must not rename files"
        # Output should mention dry-run
        assert "dry" in result.output.lower() or "would" in result.output.lower()

    def test_collision_produces_numbered_suffix(self, vault_dir, runner):
        """When a slug would collide, the second file gets a -2 suffix."""
        session_dir = vault_dir / "session"
        session_dir.mkdir(parents=True, exist_ok=True)

        uuid_a = "aaaaaaaa-0000-0000-0000-000000000001"
        uuid_b = "bbbbbbbb-0000-0000-0000-000000000002"
        # Both have identical first lines → same slug
        content = "same content here"
        for uid in (uuid_a, uuid_b):
            post = frontmatter.Post(
                content,
                id=uid,
                layer="session",
                quality_score=0.8,
                tags=[],
                access_count=0,
                content_hash="x",
            )
            (session_dir / f"{uid}.md").write_text(frontmatter.dumps(post))

        from core.cli import cli
        result = runner.invoke(
            cli,
            ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)],
        )
        assert result.exit_code == 0, f"CLI error: {result.output}"
        md_files = list(session_dir.glob("*.md"))
        assert len(md_files) == 2, f"Expected 2 files after collision rename, got {len(md_files)}"
        names = {f.name for f in md_files}
        # One file has the base slug, the other has -2 suffix
        from core.obsidian import _content_slug
        expected_slug = _content_slug(content)
        assert f"{expected_slug}.md" in names, f"{expected_slug}.md not found in {names}"
        assert f"{expected_slug}-2.md" in names, f"{expected_slug}-2.md not found in {names}"

    def test_idempotent_second_run_skips_all(self, vault_dir, runner):
        """Running the command twice should skip all files on the second run."""
        self._seed_uuid_vault(vault_dir)
        from core.cli import cli
        args = ["migrate", "--uuid-to-slug", "--vault-path", str(vault_dir)]

        # First run
        result1 = runner.invoke(cli, args)
        assert result1.exit_code == 0

        # Collect state after first run
        session_dir = vault_dir / "session"
        files_after_first = {f.name: f.stat().st_mtime for f in session_dir.glob("*.md")}

        import time
        time.sleep(0.05)

        # Second run
        result2 = runner.invoke(cli, args)
        assert result2.exit_code == 0

        # No files should have changed mtime
        files_after_second = {f.name: f.stat().st_mtime for f in session_dir.glob("*.md")}
        assert files_after_first == files_after_second, "Second run modified files"
        # Output should mention skipped
        output2 = result2.output.lower()
        assert "skip" in output2 or "0 renamed" in output2 or "already" in output2


class TestContentSlugNestedFrontmatter:
    """_content_slug handles bodies that start with nested frontmatter (e.g. ingested claude_memory files)."""

    def test_name_field_used_as_slug(self) -> None:
        from core.obsidian import _content_slug

        body = "---\nname: Devops stage not executing at end of pipeline\ntype: feedback\n---\nsome body text"
        assert _content_slug(body) == "devops-stage-not-executing-at-end"

    def test_korean_name_field(self) -> None:
        from core.obsidian import _content_slug

        body = "---\nname: mnemos 마커 문법 개정 결정\ntype: project\n---\n"
        assert _content_slug(body) == "mnemos-마커-문법-개정-결정"

    def test_nested_block_without_name_falls_back_to_body(self) -> None:
        from core.obsidian import _content_slug

        body = "---\ndescription: Something\n---\nActual body content here"
        assert _content_slug(body) == "actual-body-content-here"

    def test_nested_block_all_empty_returns_untitled(self) -> None:
        from core.obsidian import _content_slug

        body = "---\ndescription: Something\n---\n"
        assert _content_slug(body) == "untitled"

    def test_normal_content_unchanged(self) -> None:
        from core.obsidian import _content_slug

        body = "This is the first line of content"
        assert _content_slug(body) == "this-is-the-first-line-of"
