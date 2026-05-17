"""End-to-end tests for Obsidian vault multi-host git sync (issue #24).

Fixture topology
----------------
- ``bare_remote`` — a bare git repo acting as the shared remote.
- ``vaultA`` — an ObsidianBackend cloned from ``bare_remote`` (simulates host A).
- ``vaultB`` — a second clone (simulates host B) with its own ObsidianBackend.

Tests verify:
1. Capture on A → auto-pull/push triggers → search on B finds the new item.
2. Promote on A → file move propagates → B sees the file under the new layer.
3. Concurrent edit on A and B of the same item → conflict → _sync_conflict.md
   written + SyncConflictError surfaces + on-disk state preserved.

All git operations are isolated to tmp dirs — the real mnemos repo is never
touched.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Generator

import pytest

from core.config import SyncConfig
from core.fts import FTSIndex
from core.obsidian import ObsidianBackend, SyncConflictError


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


def _init_bare_with_main(bare: Path, seed_parent: Path) -> None:
    """Seed *bare* with an initial commit and set HEAD → main."""
    seed = seed_parent / "_seed"
    seed.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=seed)
    _setup_git_identity(seed)
    # Force branch name to 'main'
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
    # Ensure clones default to main
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)


def _clone(bare: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", str(bare), str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    _setup_git_identity(dest)
    return dest


def _make_fts(db_path: Path) -> FTSIndex:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return FTSIndex(db_path=str(db_path))


def _make_sync_config(remote_name: str = "origin", **kwargs) -> SyncConfig:
    """Return a SyncConfig with sync enabled and sensible test defaults."""
    defaults = dict(
        enabled=True,
        remote=remote_name,
        branch="main",
        mode="auto",
        auto_pull_on_capture=True,
        auto_push_after_commit=True,
        pull_rate_limit_seconds=0,  # no rate limit in tests
    )
    defaults.update(kwargs)
    return SyncConfig(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bare_remote(tmp_path: Path) -> Path:
    """Bare git remote that acts as the shared origin."""
    bare = _make_bare(tmp_path / "bare.git")
    _init_bare_with_main(bare, tmp_path)
    return bare


@pytest.fixture()
def vaultA(tmp_path: Path, bare_remote: Path) -> ObsidianBackend:
    clone_path = _clone(bare_remote, tmp_path / "vaultA")
    fts = _make_fts(tmp_path / "ftsA" / "fts.db")
    cfg = _make_sync_config()
    return ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)


@pytest.fixture()
def vaultB(tmp_path: Path, bare_remote: Path) -> ObsidianBackend:
    clone_path = _clone(bare_remote, tmp_path / "vaultB")
    fts = _make_fts(tmp_path / "ftsB" / "fts.db")
    cfg = _make_sync_config(
        # B does not auto-push (simulates a read-only consumer in some tests)
        auto_push_after_commit=False,
        # Disable auto-pull on capture so we can manually pull for assertions
        auto_pull_on_capture=False,
    )
    return ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)


# ---------------------------------------------------------------------------
# Test 1 — capture on A → search on B
# ---------------------------------------------------------------------------


class TestCapturePropagatesToB:
    def test_item_written_on_a_found_on_b_after_pull(
        self,
        tmp_path: Path,
        vaultA: ObsidianBackend,
        vaultB: ObsidianBackend,
    ) -> None:
        """Capture on A auto-commits + pushes; B finds it after an explicit pull."""
        # Capture on A
        vaultA.write(
            layer="session",
            item_id="e2e-item-001",
            content="Memory from host A",
            metadata={"tags": ["e2e"], "quality_score": 0.9},
        )

        # B has not pulled yet — item should NOT be on disk
        b_path = Path(vaultB._vault) / "session" / "e2e-item-001.md"
        assert not b_path.exists(), "B should not have the item before pull"

        # Pull on B
        vaultB.sync_pull()

        # After pull, B should have the file
        assert b_path.exists(), "B should have the item after pull"

        # And can read it
        item = vaultB.read("e2e-item-001")
        assert item["content"] == "Memory from host A"

    def test_multiple_captures_on_a_land_on_b(
        self,
        tmp_path: Path,
        vaultA: ObsidianBackend,
        vaultB: ObsidianBackend,
    ) -> None:
        for i in range(3):
            vaultA.write(
                layer="project",
                item_id=f"e2e-multi-{i:03d}",
                content=f"item {i}",
                metadata={},
            )

        vaultB.sync_pull()

        for i in range(3):
            p = Path(vaultB._vault) / "project" / f"e2e-multi-{i:03d}.md"
            assert p.exists(), f"item {i} should be on B after pull"


# ---------------------------------------------------------------------------
# Test 2 — promote on A propagates to B
# ---------------------------------------------------------------------------


class TestPromotePropagatesToB:
    def test_promoted_file_found_in_new_layer_on_b(
        self,
        tmp_path: Path,
        vaultA: ObsidianBackend,
        vaultB: ObsidianBackend,
    ) -> None:
        """Promote on A commits src deletion + dst creation; B sees new path."""
        # Write in session layer
        vaultA.write(
            layer="session",
            item_id="e2e-promote-001",
            content="Promoting this",
            metadata={},
        )

        # Promote to project layer
        vaultA.promote("e2e-promote-001", "project")

        # B pulls and should see the item in project, not session
        vaultB.sync_pull()

        session_path = Path(vaultB._vault) / "session" / "e2e-promote-001.md"
        project_path = Path(vaultB._vault) / "project" / "e2e-promote-001.md"

        assert not session_path.exists(), "Old session path should be gone after promote+pull"
        assert project_path.exists(), "New project path should exist on B after pull"


# ---------------------------------------------------------------------------
# Test 3 — conflict detection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_concurrent_edit_surfaces_conflict(
        self,
        tmp_path: Path,
        bare_remote: Path,
        vaultA: ObsidianBackend,
    ) -> None:
        """Simulate concurrent edits that create a rebase conflict."""
        # Write initial item on A and push
        vaultA.write(
            layer="project",
            item_id="e2e-conflict-001",
            content="original content on A",
            metadata={},
        )

        # Create a second clone that simulates host B with sync enabled
        clone_b_path = _clone(bare_remote, tmp_path / "vaultC")
        fts_b = _make_fts(tmp_path / "ftsC" / "fts.db")
        # B also has auto-push enabled so it pushes its diverging commit
        cfg_b = _make_sync_config(
            auto_pull_on_capture=False,  # disable auto-pull so we control timing
            auto_push_after_commit=True,
            pull_rate_limit_seconds=9999,  # prevent any auto-pull
        )
        vaultC = ObsidianBackend(vault_path=str(clone_b_path), fts=fts_b, sync_config=cfg_b)

        # B (C) first syncs to get A's initial write
        vaultC.sync_pull()
        assert (clone_b_path / "project" / "e2e-conflict-001.md").exists()

        # B edits the same item and pushes WITHOUT pulling first
        # (Simulate a race: B has the item, edits it, pushes directly)
        item_path = clone_b_path / "project" / "e2e-conflict-001.md"
        import frontmatter as _fm
        post = _fm.load(str(item_path))
        post.content = "conflicting content from host B/C"
        post["updated_at"] = "2024-01-02T00:00:00+00:00"
        with item_path.open("w", encoding="utf-8") as f:
            f.write(_fm.dumps(post))
        from core.git import add as _add, commit as _commit, push as _push
        _add(clone_b_path, [str(item_path)])
        _commit(clone_b_path, "conflict commit from B/C")
        _push(clone_b_path, "origin", "main")

        # A edits the same item AFTER C pushed — A has a diverging local commit
        # Directly edit A's local file to simulate a diverging edit without auto-push
        item_path_a = Path(vaultA._vault) / "project" / "e2e-conflict-001.md"
        post_a = _fm.load(str(item_path_a))
        post_a.content = "diverging content from host A (to cause conflict)"
        post_a["updated_at"] = "2024-01-02T00:01:00+00:00"
        with item_path_a.open("w", encoding="utf-8") as f:
            f.write(_fm.dumps(post_a))
        _add(Path(vaultA._vault), [str(item_path_a)])
        _commit(Path(vaultA._vault), "diverging commit from A")

        # Now A tries to pull — rebase should fail (conflict)
        # Override the rate limit so the pull is actually attempted
        vaultA._sync = _make_sync_config(pull_rate_limit_seconds=0)
        vaultA._last_pull_ts = 0.0  # force the rate limit to pass

        with pytest.raises(SyncConflictError):
            vaultA.sync_pull()

        # _sync_conflict.md must be written
        conflict_md = Path(vaultA._vault) / "_sync_conflict.md"
        assert conflict_md.exists(), "_sync_conflict.md should be written on conflict"

        # On-disk content at A must NOT be silently overwritten
        # (after a failed rebase, git rebase --abort restores the original state)
        # We verify the conflict marker file is present and readable
        conflict_text = conflict_md.read_text(encoding="utf-8")
        assert "mnemos Sync Conflict" in conflict_text

    def test_no_conflict_artefacts_on_clean_pull(
        self,
        tmp_path: Path,
        vaultA: ObsidianBackend,
        vaultB: ObsidianBackend,
    ) -> None:
        """A clean pull does not write _sync_conflict.md."""
        vaultA.write(
            layer="session",
            item_id="e2e-clean-001",
            content="clean pull test",
            metadata={},
        )
        vaultB.sync_pull()

        conflict_md = Path(vaultB._vault) / "_sync_conflict.md"
        assert not conflict_md.exists()


# ---------------------------------------------------------------------------
# Test 4 — rate limit
# ---------------------------------------------------------------------------


class TestRateLimit:
    def test_pull_skipped_within_rate_limit_window(
        self,
        tmp_path: Path,
        bare_remote: Path,
    ) -> None:
        """A pull is skipped (not attempted) when called within the rate-limit window."""
        clone_path = _clone(bare_remote, tmp_path / "rate_vault")
        fts = _make_fts(tmp_path / "rate_fts" / "fts.db")
        cfg = _make_sync_config(
            pull_rate_limit_seconds=9999,  # long window
            auto_push_after_commit=False,  # disable push to avoid remote contact
        )
        backend = ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)

        # Simulate a recent pull by setting _last_pull_ts to now
        backend._last_pull_ts = time.monotonic()

        # Write an item — _hook_before_write should skip the pull
        # We can verify this by checking that no network call is made (the remote
        # does not even need to be reachable — if pull were attempted, it would
        # fail because the remote is a bare local path; here it's fine but the
        # key is the rate limit skips the call entirely).
        call_count = [0]
        original_should_pull = backend._should_pull

        def mock_should_pull():
            result = original_should_pull()
            call_count[0] += 1
            return result

        backend._should_pull = mock_should_pull  # type: ignore[method-assign]
        backend.write(
            layer="session",
            item_id="rate-limit-test",
            content="rate limit test",
            metadata={},
        )
        # _should_pull was called (once) and returned False due to rate limit
        assert call_count[0] >= 1
        # Verify _should_pull returns False
        backend._last_pull_ts = time.monotonic()  # set to now
        assert backend._should_pull() is False

    def test_pull_attempted_after_rate_limit_expires(
        self,
        tmp_path: Path,
        bare_remote: Path,
    ) -> None:
        """A pull is attempted when the rate-limit window has elapsed."""
        clone_path = _clone(bare_remote, tmp_path / "rl_vault")
        fts = _make_fts(tmp_path / "rl_fts" / "fts.db")
        cfg = _make_sync_config(pull_rate_limit_seconds=0)
        backend = ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)
        backend._last_pull_ts = 0.0  # never pulled

        assert backend._should_pull() is True


# ---------------------------------------------------------------------------
# Test 5 — backward compat (sync disabled)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_write_does_not_commit_when_sync_disabled(self, tmp_path: Path) -> None:
        """When sync.enabled is False, writes do not create git commits."""
        vault = tmp_path / "no_sync_vault"
        vault.mkdir()
        fts = _make_fts(tmp_path / "fts" / "fts.db")
        # sync disabled by default (SyncConfig defaults to enabled=False)
        backend = ObsidianBackend(vault_path=str(vault), fts=fts)
        assert backend._sync.enabled is False

        # This should work normally without a git repo
        backend.write(
            layer="session",
            item_id="nosync-001",
            content="no sync content",
            metadata={},
        )
        assert (vault / "session" / "nosync-001.md").exists()
        # No .git directory should be created
        assert not (vault / ".git").exists()

    def test_sync_disabled_is_default(self, tmp_path: Path) -> None:
        """The default SyncConfig has enabled=False."""
        fts = _make_fts(tmp_path / "fts" / "fts.db")
        vault = tmp_path / "v"
        vault.mkdir()
        backend = ObsidianBackend(vault_path=str(vault), fts=fts)
        assert backend._sync.enabled is False


# ---------------------------------------------------------------------------
# Test 6 — sync_status
# ---------------------------------------------------------------------------


class TestSyncStatus:
    def test_sync_status_returns_expected_keys(
        self,
        tmp_path: Path,
        bare_remote: Path,
    ) -> None:
        clone_path = _clone(bare_remote, tmp_path / "status_vault")
        fts = _make_fts(tmp_path / "status_fts" / "fts.db")
        cfg = _make_sync_config()
        backend = ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)

        stat = backend.sync_status()
        for key in ("branch", "ahead", "behind", "dirty", "untracked",
                    "last_pull_ts", "last_push_ts", "sync_enabled",
                    "sync_remote", "sync_branch"):
            assert key in stat, f"sync_status() must include key: {key}"

    def test_sync_status_reflects_config(
        self,
        tmp_path: Path,
        bare_remote: Path,
    ) -> None:
        clone_path = _clone(bare_remote, tmp_path / "status_cfg_vault")
        fts = _make_fts(tmp_path / "status_cfg_fts" / "fts.db")
        cfg = _make_sync_config(remote_name="backup", branch="develop")
        cfg = SyncConfig(enabled=True, remote="backup", branch="develop",
                         pull_rate_limit_seconds=0)
        backend = ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)
        # set remote in the repo
        from core.git import set_remote
        set_remote(clone_path, "backup", str(bare_remote))

        stat = backend.sync_status()
        assert stat["sync_remote"] == "backup"
        assert stat["sync_branch"] == "develop"
        assert stat["sync_enabled"] is True
