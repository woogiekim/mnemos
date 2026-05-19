"""Unit tests for ``core/git.py`` — thin git wrapper.

All tests use isolated ``tmp_path`` git repos and never touch the real
repository.  A bare remote repo is created via a second ``tmp_path``
fixture to test push/pull operations.

TDD: written before the implementation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.git import (
    GitCommandError,
    GitNotFoundError,
    add,
    commit,
    current_branch,
    fetch,
    init,
    pull_rebase,
    push,
    rebase_onto,
    remote_exists,
    set_remote,
    status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _setup_repo(path: Path, initial_commit: bool = True) -> Path:
    """Initialise a fresh git repo with a user identity configured."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=path)
    _git("config", "user.email", "test@mnemos.test", cwd=path)
    _git("config", "user.name", "mnemos Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)
    if initial_commit:
        (path / "README.md").write_text("# vault\n")
        _git("add", ".", cwd=path)
        _git("commit", "-m", "init", cwd=path)
    return path


def _setup_bare(path: Path, seed_dir: Path | None = None) -> Path:
    """Create a bare git repo with an initial commit on 'main'.

    We create a standalone (non-bare) repo with a ``main`` branch, commit
    into it, and then push to the bare repo — avoiding the "master" default
    branch name problem on systems where ``init.defaultBranch`` is not set.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", cwd=path)

    # Create a standalone seed repo (not a clone of the bare)
    if seed_dir is None:
        seed_dir = path.parent / "_seed_bare"
    seed_dir.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=seed_dir)
    _git("config", "user.email", "test@mnemos.test", cwd=seed_dir)
    _git("config", "user.name", "mnemos Test", cwd=seed_dir)
    _git("config", "commit.gpgsign", "false", cwd=seed_dir)
    # Force the branch to be named 'main'
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=str(seed_dir),
        capture_output=True,
        text=True,
    )
    _git("remote", "add", "origin", str(path), cwd=seed_dir)
    (seed_dir / ".gitkeep").write_text("")
    _git("add", ".", cwd=seed_dir)
    _git("commit", "-m", "init", cwd=seed_dir)
    _git("push", "-u", "origin", "main", cwd=seed_dir)
    # Set the bare repo's HEAD to point to main so that git clone checks out main
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=path)
    return path


def _clone(bare: Path, dest: Path) -> Path:
    """Clone *bare* into *dest*."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(bare), str(dest)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git("config", "user.email", "test@mnemos.test", cwd=dest)
    _git("config", "user.name", "mnemos Test", cwd=dest)
    _git("config", "commit.gpgsign", "false", cwd=dest)
    return dest


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_creates_git_dir(self, tmp_path):
        repo = tmp_path / "vault"
        repo.mkdir()
        init(repo)
        assert (repo / ".git").is_dir()

    def test_init_is_idempotent(self, tmp_path):
        repo = tmp_path / "vault"
        repo.mkdir()
        init(repo)
        init(repo)  # second call must not raise
        assert (repo / ".git").is_dir()


# ---------------------------------------------------------------------------
# set_remote / remote_exists
# ---------------------------------------------------------------------------


class TestRemote:
    def test_set_remote_adds_new(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        set_remote(repo, "origin", "https://example.com/vault.git")
        assert remote_exists(repo, "origin")

    def test_set_remote_updates_url(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        set_remote(repo, "origin", "https://old.example.com/vault.git")
        set_remote(repo, "origin", "https://new.example.com/vault.git")
        rc, out, _ = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).returncode, *[None, None]
        # re-run properly
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "https://new.example.com/vault.git"

    def test_remote_exists_false_for_missing(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        assert not remote_exists(repo, "upstream")

    def test_remote_exists_true_after_set(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        set_remote(repo, "backup", "https://example.com/backup.git")
        assert remote_exists(repo, "backup")


# ---------------------------------------------------------------------------
# current_branch
# ---------------------------------------------------------------------------


class TestCurrentBranch:
    def test_returns_branch_name(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        branch = current_branch(repo)
        assert branch in ("main", "master")

    def test_raises_on_empty_repo(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        init(repo)
        # No commits yet — HEAD is unborn
        with pytest.raises(GitCommandError):
            current_branch(repo)


# ---------------------------------------------------------------------------
# add / commit
# ---------------------------------------------------------------------------


class TestAddCommit:
    def test_commit_returns_true_when_changes_exist(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        (repo / "note.md").write_text("hello")
        add(repo, ["note.md"])
        result = commit(repo, "add note")
        assert result is True

    def test_commit_returns_false_when_nothing_to_commit(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        result = commit(repo, "nothing to commit")
        assert result is False

    def test_add_empty_list_is_noop(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        add(repo, [])  # must not raise

    def test_commit_message_appears_in_log(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        (repo / "item.md").write_text("content")
        add(repo, ["item.md"])
        commit(repo, "mnemos: session abc123 (2024-01-01T00:00:00Z)")
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert "mnemos: session abc123" in log.stdout

    def test_add_raises_on_nonexistent_file(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        with pytest.raises(GitCommandError):
            add(repo, ["does-not-exist.md"])


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_returns_expected_keys(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        stat = status(repo)
        assert "branch" in stat
        assert "ahead" in stat
        assert "behind" in stat
        assert "dirty" in stat
        assert "untracked" in stat

    def test_dirty_true_when_uncommitted_changes(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        (repo / "dirty.md").write_text("uncommitted")
        add(repo, ["dirty.md"])
        stat = status(repo)
        assert stat["dirty"] is True

    def test_dirty_false_on_clean_repo(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        stat = status(repo)
        assert stat["dirty"] is False

    def test_untracked_count(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        (repo / "untracked.md").write_text("hello")
        stat = status(repo)
        assert stat["untracked"] >= 1


# ---------------------------------------------------------------------------
# push / pull_rebase — require a bare remote
# ---------------------------------------------------------------------------


class TestPushPull:
    def test_push_to_bare_remote(self, tmp_path):
        bare = _setup_bare(tmp_path / "bare.git")
        repo = _clone(bare, tmp_path / "clone")
        (repo / "item.md").write_text("new memory")
        add(repo, ["item.md"])
        commit(repo, "add item")
        push(repo, "origin", "main")

        # Verify the bare remote has the commit
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(bare),
            capture_output=True,
            text=True,
        )
        assert "add item" in log.stdout

    def test_pull_rebase_from_bare_remote(self, tmp_path):
        bare = _setup_bare(tmp_path / "bare.git")
        repoA = _clone(bare, tmp_path / "A")
        repoB = _clone(bare, tmp_path / "B")

        # Commit on A and push
        (repoA / "shared.md").write_text("from A")
        add(repoA, ["shared.md"])
        commit(repoA, "add shared from A")
        push(repoA, "origin", "main")

        # Pull on B
        pull_rebase(repoB, "origin", "main")
        assert (repoB / "shared.md").exists()

    def test_push_raises_git_command_error_on_bad_remote(self, tmp_path):
        repo = _setup_repo(tmp_path / "repo")
        set_remote(repo, "origin", "/nonexistent/path.git")
        with pytest.raises(GitCommandError):
            push(repo, "origin", "main")

    def test_pull_rebase_with_autostash(self, tmp_path):
        """Pull with --autostash should re-apply local dirty changes."""
        bare = _setup_bare(tmp_path / "bare.git")
        repoA = _clone(bare, tmp_path / "A")
        repoB = _clone(bare, tmp_path / "B")

        # Commit something on A and push
        (repoA / "alpha.md").write_text("alpha")
        add(repoA, ["alpha.md"])
        commit(repoA, "add alpha")
        push(repoA, "origin", "main")

        # Make B dirty (unstaged change to a different file)
        (repoB / "local.md").write_text("local change")

        # Pull with autostash — must succeed even though B is dirty
        pull_rebase(repoB, "origin", "main", autostash=True)
        assert (repoB / "alpha.md").exists()


# ---------------------------------------------------------------------------
# fetch / rebase_onto
# ---------------------------------------------------------------------------


class TestFetchRebaseOnto:
    def test_fetch_single_branch_writes_one_fetch_head_entry(self, tmp_path):
        """Fetching a single branch must write exactly one line to FETCH_HEAD.

        This is the regression test for the ``fatal: Cannot rebase onto
        multiple branches`` bug.  The original ``git pull --rebase origin main``
        could leave multiple FETCH_HEAD entries when the remote had several refs
        (e.g. other branches), causing the next ``git pull --rebase`` to fail.

        With ``git fetch origin main`` (single-ref fetch) only one entry is
        written, eliminating the ambiguity.
        """
        bare = _setup_bare(tmp_path / "bare.git")
        # Add a second branch on the bare remote so it has >1 ref
        seed2 = tmp_path / "_seed2"
        _clone(bare, seed2)
        (seed2 / "other.md").write_text("other branch content")
        _git("checkout", "-b", "other-branch", cwd=seed2)
        _git("add", ".", cwd=seed2)
        _git("commit", "-m", "add other branch", cwd=seed2)
        _git("push", "origin", "other-branch", cwd=seed2)

        repo = _clone(bare, tmp_path / "repo")
        # Single-branch fetch: FETCH_HEAD must have exactly one entry
        fetch(repo, "origin", branch="main")
        fetch_head = (repo / ".git" / "FETCH_HEAD").read_text()
        entries = [line for line in fetch_head.splitlines() if line.strip()]
        assert len(entries) == 1, (
            f"Expected 1 FETCH_HEAD entry after single-branch fetch, got {len(entries)}: "
            f"{fetch_head!r}"
        )

    def test_pull_rebase_succeeds_after_multi_branch_remote(self, tmp_path):
        """pull_rebase must NOT fail when the remote has multiple branches.

        Regression test: the old ``git pull --rebase origin main`` wrote all
        remote refs into FETCH_HEAD on the first invocation.  When called a
        second time the rebase step failed with
        ``fatal: Cannot rebase onto multiple branches``.

        The new fetch+rebase implementation fetches only ``main``, so the
        second call succeeds regardless of how many branches exist on the remote.
        """
        bare = _setup_bare(tmp_path / "bare.git")

        # Add extra branches to simulate a real-world remote with many refs
        seed_extra = tmp_path / "_seed_extra"
        _clone(bare, seed_extra)
        for branch_name in ("dev", "feature/x", "feature/y"):
            _git("checkout", "-b", branch_name, cwd=seed_extra)
            (seed_extra / f"{branch_name.replace('/', '-')}.md").write_text(branch_name)
            _git("add", ".", cwd=seed_extra)
            _git("commit", "-m", f"add {branch_name}", cwd=seed_extra)
            _git("push", "origin", branch_name, cwd=seed_extra)
        _git("checkout", "main", cwd=seed_extra)

        repoA = _clone(bare, tmp_path / "A")
        repoB = _clone(bare, tmp_path / "B")

        # First pull_rebase — fetches and rebases successfully
        pull_rebase(repoB, "origin", "main")

        # Push a new commit from A so B is behind
        (repoA / "new.md").write_text("from A")
        add(repoA, ["new.md"])
        commit(repoA, "add new from A")
        push(repoA, "origin", "main")

        # Second pull_rebase — must NOT raise even though FETCH_HEAD was set
        # by the first pull_rebase call (the old code left multiple entries here)
        pull_rebase(repoB, "origin", "main")
        assert (repoB / "new.md").exists()

    def test_rebase_onto_explicit_ref(self, tmp_path):
        """rebase_onto with an explicit remote-tracking ref succeeds."""
        bare = _setup_bare(tmp_path / "bare.git")
        repoA = _clone(bare, tmp_path / "A")
        repoB = _clone(bare, tmp_path / "B")

        # A pushes a new commit
        (repoA / "rebase_target.md").write_text("target")
        add(repoA, ["rebase_target.md"])
        commit(repoA, "add rebase_target")
        push(repoA, "origin", "main")

        # B fetches (so origin/main is updated) then rebases onto origin/main
        fetch(repoB, "origin", branch="main")
        rebase_onto(repoB, "origin/main")
        assert (repoB / "rebase_target.md").exists()

    def test_fetch_without_branch_fetches_all_refs(self, tmp_path):
        """Calling fetch without branch= fetches all refs (broad fetch)."""
        bare = _setup_bare(tmp_path / "bare.git")

        # Push a second branch to the remote
        seed2 = tmp_path / "_seed_b"
        _clone(bare, seed2)
        _git("checkout", "-b", "extra", cwd=seed2)
        (seed2 / "extra.md").write_text("extra")
        _git("add", ".", cwd=seed2)
        _git("commit", "-m", "add extra", cwd=seed2)
        _git("push", "origin", "extra", cwd=seed2)

        repo = _clone(bare, tmp_path / "repo")
        # Broad fetch — must not raise; FETCH_HEAD may have >1 entry (OK here,
        # we just verify the call completes successfully)
        fetch(repo, "origin")  # no branch= argument
        # The remote-tracking ref for extra must be present after broad fetch
        rc, out, _ = subprocess.run(
            ["git", "branch", "-r"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).returncode, *[None, None]
        result = subprocess.run(
            ["git", "branch", "-r"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )
        assert "origin/extra" in result.stdout


# ---------------------------------------------------------------------------
# GitNotFoundError
# ---------------------------------------------------------------------------


class TestGitNotFoundError:
    def test_raises_when_git_not_on_path(self, tmp_path, monkeypatch):
        """When git is not on PATH, every function raises GitNotFoundError."""
        monkeypatch.setenv("PATH", "")
        # shutil.which re-reads PATH on every call so clearing it is enough
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)

        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(GitNotFoundError):
            init(repo)
