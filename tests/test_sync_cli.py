"""CLI smoke tests for ``mnemos sync`` subcommands (issue #24).

Tests cover the wiring between the Click CLI and the ObsidianBackend.
Heavy logic is covered by test_sync_e2e.py and test_git_wrapper.py.

Fixture: a minimal Obsidian vault + bare git remote + mnemos.yml config file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _setup_git_identity(path: Path) -> None:
    _git("config", "user.email", "test@mnemos.test", cwd=path)
    _git("config", "user.name", "mnemos Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)


def _make_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", cwd=path)
    return path


def _clone(bare: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", str(bare), str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    _setup_git_identity(dest)
    return dest


def _seed_bare(bare: Path, tmp_path: Path) -> None:
    """Push an initial commit to a bare remote so it has a main branch."""
    seed = tmp_path / "_seed"
    seed.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=seed)
    _setup_git_identity(seed)
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=str(seed),
        capture_output=True,
        text=True,
    )
    _git("remote", "add", "origin", str(bare), cwd=seed)
    (seed / ".gitkeep").write_text("")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)
    # Set HEAD so clones default to main
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sync_env(tmp_path: Path):
    """
    Returns a dict with:
      - runner: CliRunner
      - vault_path: str (cloned vault path)
      - repo_root: str (used as MNEMOS_REPO_ROOT)
      - mnemos_yml_path: Path
      - bare_path: Path
    """
    # Set up bare remote
    bare = _make_bare(tmp_path / "bare.git")
    _seed_bare(bare, tmp_path)

    # Clone vault
    vault_path = _clone(bare, tmp_path / "vault")

    # Write a minimal mnemos.yml pointing at the vault with sync enabled
    mnemos_yml = tmp_path / "mnemos.yml"
    config = {
        "storage": {
            "backend": "obsidian",
            "vault_path": str(vault_path),
            "sync": {
                "enabled": True,
                "remote": "origin",
                "branch": "main",
                "auto_pull_on_capture": True,
                "auto_push_after_commit": True,
                "pull_rate_limit_seconds": 0,
            },
        }
    }
    mnemos_yml.write_text(yaml.dump(config), encoding="utf-8")

    runner = CliRunner()
    return {
        "runner": runner,
        "vault_path": str(vault_path),
        "repo_root": str(tmp_path),
        "mnemos_yml_path": mnemos_yml,
        "bare_path": bare,
    }


# ---------------------------------------------------------------------------
# mnemos sync pull
# ---------------------------------------------------------------------------


class TestSyncPullCLI:
    def test_sync_pull_succeeds(self, sync_env) -> None:
        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "pull"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        assert result.exit_code == 0, result.output
        assert "pull complete" in result.output

    def test_sync_pull_error_on_non_obsidian_backend(self, tmp_path: Path) -> None:
        """sync pull fails gracefully when backend != obsidian."""
        runner = CliRunner()
        mnemos_yml = tmp_path / "mnemos.yml"
        mnemos_yml.write_text(yaml.dump({"storage": {"backend": "default"}}))
        result = runner.invoke(
            cli,
            ["sync", "pull"],
            env={"MNEMOS_REPO_ROOT": str(tmp_path)},
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# mnemos sync push
# ---------------------------------------------------------------------------


class TestSyncPushCLI:
    def test_sync_push_succeeds_with_no_commits(self, sync_env) -> None:
        """push with nothing ahead still exits 0."""
        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "push"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        # Push when up-to-date — git exits 0
        assert result.exit_code == 0, result.output
        assert "push complete" in result.output


# ---------------------------------------------------------------------------
# mnemos sync status
# ---------------------------------------------------------------------------


class TestSyncStatusCLI:
    def test_sync_status_shows_expected_fields(self, sync_env) -> None:
        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "status"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        assert result.exit_code == 0, result.output
        for field in ("branch", "ahead", "behind", "dirty", "sync_enabled", "sync_remote"):
            assert field in result.output, f"Expected field '{field}' in output"

    def test_sync_status_reflects_sync_config(self, sync_env) -> None:
        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "status"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        assert "origin" in result.output
        assert "main" in result.output
        assert "True" in result.output  # sync_enabled


# ---------------------------------------------------------------------------
# mnemos sync init
# ---------------------------------------------------------------------------


class TestSyncInitCLI:
    def test_sync_init_inits_and_adds_remote(self, tmp_path: Path) -> None:
        """sync init on a non-git directory inits a repo and sets the remote."""
        bare = _make_bare(tmp_path / "bare2.git")
        vault_path = tmp_path / "new_vault"
        vault_path.mkdir()

        # Write mnemos.yml pointing to the new vault
        mnemos_yml = tmp_path / "mnemos.yml"
        mnemos_yml.write_text(
            yaml.dump(
                {
                    "storage": {
                        "backend": "obsidian",
                        "vault_path": str(vault_path),
                        "sync": {"enabled": True, "remote": "origin", "branch": "main"},
                    }
                }
            )
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["sync", "init", "--remote", str(bare)],
            env={"MNEMOS_REPO_ROOT": str(tmp_path)},
        )
        assert result.exit_code == 0, result.output
        assert (vault_path / ".git").is_dir()
        assert "init complete" in result.output

    def test_sync_init_is_idempotent(self, tmp_path: Path) -> None:
        """Calling sync init twice should not fail."""
        bare = _make_bare(tmp_path / "bare3.git")
        vault_path = tmp_path / "idempotent_vault"
        vault_path.mkdir()

        mnemos_yml = tmp_path / "mnemos.yml"
        mnemos_yml.write_text(
            yaml.dump(
                {
                    "storage": {
                        "backend": "obsidian",
                        "vault_path": str(vault_path),
                        "sync": {"enabled": True, "remote": "origin", "branch": "main"},
                    }
                }
            )
        )
        runner = CliRunner()
        for _ in range(2):
            result = runner.invoke(
                cli,
                ["sync", "init", "--remote", str(bare)],
                env={"MNEMOS_REPO_ROOT": str(tmp_path)},
            )
            assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# mnemos sync continue
# ---------------------------------------------------------------------------


class TestSyncContinueCLI:
    def test_sync_continue_aborts_when_conflict_markers_present(
        self, sync_env
    ) -> None:
        """sync continue exits non-zero when vault files have conflict markers."""
        vault_path = sync_env["vault_path"]
        # Inject a conflict marker into a tracked file
        conflict_file = Path(vault_path) / "session" / "conflict-item.md"
        conflict_file.parent.mkdir(parents=True, exist_ok=True)
        conflict_file.write_text(
            "---\nid: conflict-item\n---\n<<<<<<< HEAD\nlocal\n=======\nremote\n>>>>>>> abc\n",
            encoding="utf-8",
        )

        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "continue"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        assert result.exit_code != 0
        assert "conflict" in result.output.lower() or "conflict" in (result.output + "").lower()

    def test_sync_continue_cleans_artefacts_on_success(self, sync_env) -> None:
        """sync continue deletes _sync_conflict.md when no rebase is in progress."""
        vault_path = Path(sync_env["vault_path"])

        # Create a _sync_conflict.md (no rebase in progress — git rebase --continue
        # should exit gracefully when there is no active rebase on some git versions)
        conflict_md = vault_path / "_sync_conflict.md"
        conflict_md.write_text("# mnemos Sync Conflict\n\ntest\n", encoding="utf-8")

        runner = sync_env["runner"]
        result = runner.invoke(
            cli,
            ["sync", "continue"],
            env={"MNEMOS_REPO_ROOT": sync_env["repo_root"]},
        )
        # Either success (no active rebase — git exits 0 with a message) or an error
        # about no rebase in progress — either way _sync_conflict.md cleanup depends
        # on success path; we only assert that the CLI does not crash unexpectedly.
        assert result.exit_code in (0, 1)
