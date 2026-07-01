"""Tests for default-backend remote git-sync (issue #69).

The default :class:`core.store.MemoryStore` backend gains OPTIONAL remote
git-sync, extracted into the shared :class:`core.sync.GitSyncEngine` and shared
with the Obsidian backend (DRY/DIP).  These tests cover:

- write() → wiki-scoped commit + push (auto mode)
- auto_pull_on_capture rate-limit (skip path and pass path)
- local-only silent skip when no remote
- conflict → SyncConflictError + wiki/_sync_conflict.md
- sync.enabled=false → hooks inert (no commit)
- wiki-scope filter: a non-wiki write produces no commit; `git add .` is never used
- gateway integration: default backend + sync config → capture creates a wiki commit
- CLI: mnemos sync pull/push/status/init/continue on the default backend
- every branch of core/sync.py (push skip, remote_has_branch False, etc.)

All git operations are isolated to tmp dirs — the real repo is never touched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Generator

import pytest

import core.git as git
from core.config import SyncConfig
from core.store import MemoryStore, SyncableBackend, SyncConflictError
from core.sync import GitSyncEngine, hash_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _setup_identity(path: Path) -> None:
    _git("config", "user.email", "test@mnemos.test", cwd=path)
    _git("config", "user.name", "mnemos Test", cwd=path)
    _git("config", "commit.gpgsign", "false", cwd=path)


def _make_bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "--bare", cwd=path)
    return path


def _init_repo_with_main(repo: Path, bare: Path | None = None) -> None:
    """Initialise *repo* on branch ``main`` with one commit (and optional remote)."""
    repo.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=repo)
    _setup_identity(repo)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=str(repo), capture_output=True, text=True)
    # Seed a root file so HEAD exists and so a non-wiki path is tracked.
    (repo / "README.md").write_text("root readme\n", encoding="utf-8")
    (repo / "wiki").mkdir(parents=True, exist_ok=True)
    (repo / "wiki" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    if bare is not None:
        _git("remote", "add", "origin", str(bare), cwd=repo)
        _git("push", "-u", "origin", "main", cwd=repo)
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)


def _sync_config(**kwargs) -> SyncConfig:
    defaults = dict(
        enabled=True,
        remote="origin",
        branch="main",
        mode="auto",
        auto_pull_on_capture=True,
        auto_push_after_commit=True,
        pull_rate_limit_seconds=0,
    )
    defaults.update(kwargs)
    return SyncConfig(**defaults)


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo), capture_output=True, text=True,
    )
    return int(out.stdout.strip() or "0")


def _git_log_messages(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=str(repo), capture_output=True, text=True,
    )
    return [l for l in out.stdout.splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def bare_remote(tmp_path: Path) -> Path:
    return _make_bare(tmp_path / "bare.git")


@pytest.fixture()
def repo_with_remote(tmp_path: Path, bare_remote: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo_with_main(repo, bare=bare_remote)
    return repo


@pytest.fixture()
def repo_local_only(tmp_path: Path) -> Path:
    repo = tmp_path / "local"
    _init_repo_with_main(repo, bare=None)
    return repo


@pytest.fixture(autouse=True)
def _isolate_pull_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the per-root pull-timestamp cache into the tmp dir."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# write() → commit + push
# ---------------------------------------------------------------------------


def test_write_creates_wiki_commit_and_pushes(repo_with_remote: Path, bare_remote: Path) -> None:
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    before = _commit_count(repo_with_remote)
    store.write("project", "alpha", "hello world", {})
    after = _commit_count(repo_with_remote)
    assert after == before + 1
    # Commit message uses the template (layer/id present).
    msgs = _git_log_messages(repo_with_remote)
    assert any("project" in m and "alpha" in m for m in msgs)
    # The commit was pushed to the bare remote.
    assert git.remote_has_branch(repo_with_remote, "origin", "main")
    remote_log = subprocess.run(
        ["git", "log", "--pretty=%s", "main"], cwd=str(bare_remote),
        capture_output=True, text=True,
    )
    assert any("alpha" in m for m in remote_log.stdout.splitlines())


def test_write_stages_only_wiki_paths(repo_with_remote: Path) -> None:
    """A non-wiki write (session layer under .agent/) produces no commit."""
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    before = _commit_count(repo_with_remote)
    # session layer lives under .agent/sessions (gitignored / outside wiki).
    store.write("session", "s1", "ephemeral note", {}, session_id="sess")
    after = _commit_count(repo_with_remote)
    assert after == before  # nothing under wiki/ changed → commit no-op


def test_git_add_never_called_with_dot(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decision 2: the default backend never stages the repo root with '.'."""
    seen_adds: list[list[str]] = []
    real_add = git.add

    def spy_add(path, files):
        seen_adds.append([str(f) for f in files])
        return real_add(path, files)

    monkeypatch.setattr("core.sync._git.add", spy_add)
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    store.write("global", "g1", "global note", {})
    assert seen_adds, "expected at least one git add for a wiki write"
    for files in seen_adds:
        assert "." not in files
        for f in files:
            assert "wiki" in f  # only wiki paths are staged


# ---------------------------------------------------------------------------
# rate limit
# ---------------------------------------------------------------------------


def test_rate_limit_skips_pull(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A high rate-limit window suppresses the pre-write pull."""
    pulls: list[int] = []
    monkeypatch.setattr(
        "core.sync._git.pull_rebase",
        lambda *a, **k: pulls.append(1),
    )
    cfg = _sync_config(pull_rate_limit_seconds=10_000)
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=cfg)
    # Prime _last_pull_ts to "now" so the window has not elapsed.
    import time
    store._sync_engine._last_pull_ts = time.monotonic()
    store.write("project", "rl", "x", {})
    assert pulls == []  # pull skipped by rate limit


def test_rate_limit_elapsed_triggers_pull(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pulls: list[int] = []
    monkeypatch.setattr(
        "core.sync._git.pull_rebase",
        lambda *a, **k: pulls.append(1),
    )
    cfg = _sync_config(pull_rate_limit_seconds=0)
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=cfg)
    store.write("project", "rl2", "x", {})
    assert pulls == [1]  # rate limit elapsed → pull attempted


# ---------------------------------------------------------------------------
# local-only fallback
# ---------------------------------------------------------------------------


def test_local_only_no_remote_silent_skip(repo_local_only: Path) -> None:
    """No remote configured → pull/push skipped, but local commit still happens."""
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    before = _commit_count(repo_local_only)
    store.write("project", "loc", "local note", {})
    after = _commit_count(repo_local_only)
    assert after == before + 1  # local commit created
    # No remote → has_remote() is False.
    assert store._sync_engine.has_remote() is False


def test_sync_pull_push_silent_without_remote(repo_local_only: Path) -> None:
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    # Both return silently (no remote) — no exception.
    store.sync_pull()
    store.sync_push()


# ---------------------------------------------------------------------------
# conflict surfacing
# ---------------------------------------------------------------------------


def test_pull_conflict_raises_and_writes_artefact(
    repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a, **k):
        raise git.GitCommandError(1, "merge conflict", ["git", "rebase"])

    monkeypatch.setattr("core.sync._git.pull_rebase", boom)
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    with pytest.raises(SyncConflictError):
        store.write("project", "conf", "x", {})
    artefact = repo_with_remote / "wiki" / "_sync_conflict.md"
    assert artefact.exists()
    assert "Sync Conflict" in artefact.read_text(encoding="utf-8")


def test_sync_pull_conflict_raises(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise git.GitCommandError(1, "rebase conflict", ["git", "rebase"])

    monkeypatch.setattr("core.sync._git.pull_rebase", boom)
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    with pytest.raises(SyncConflictError):
        store.sync_pull()
    assert (repo_with_remote / "wiki" / "_sync_conflict.md").exists()


# ---------------------------------------------------------------------------
# sync disabled → inert
# ---------------------------------------------------------------------------


def test_sync_disabled_hooks_inert(repo_with_remote: Path) -> None:
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=SyncConfig(enabled=False))
    before = _commit_count(repo_with_remote)
    store.write("project", "noop", "x", {})
    after = _commit_count(repo_with_remote)
    assert after == before  # disabled → no commit


def test_default_store_no_sync_config_inert(repo_with_remote: Path) -> None:
    store = MemoryStore(repo_root=str(repo_with_remote))  # no sync_config
    assert store._sync.enabled is False
    before = _commit_count(repo_with_remote)
    store.write("project", "noop2", "x", {})
    assert _commit_count(repo_with_remote) == before


# ---------------------------------------------------------------------------
# SyncableBackend protocol
# ---------------------------------------------------------------------------


def test_memorystore_is_syncable_backend(repo_local_only: Path) -> None:
    store = MemoryStore(repo_root=str(repo_local_only))
    assert isinstance(store, SyncableBackend)


# ---------------------------------------------------------------------------
# public sync API
# ---------------------------------------------------------------------------


def test_sync_status_reports_config(repo_with_remote: Path) -> None:
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config(remote="origin", branch="main"))
    stat = store.sync_status()
    assert stat["sync_enabled"] is True
    assert stat["sync_remote"] == "origin"
    assert stat["sync_branch"] == "main"
    assert stat["last_pull_ts"] == 0.0
    assert stat["last_push_ts"] == 0.0


def test_sync_commit_stages_wiki(repo_local_only: Path) -> None:
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    # Create an uncommitted wiki file directly.
    wiki_proj = repo_local_only / "wiki" / "projects"
    wiki_proj.mkdir(parents=True, exist_ok=True)
    (wiki_proj / "manual.md").write_text("manual\n", encoding="utf-8")
    before = _commit_count(repo_local_only)
    created = store.sync_commit(message="manual commit")
    assert created is True
    assert _commit_count(repo_local_only) == before + 1


def test_sync_commit_clean_tree_returns_false(repo_local_only: Path) -> None:
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    assert store.sync_commit() is False  # nothing pending under wiki/


def test_sync_commit_never_stages_observability_log(repo_local_only: Path) -> None:
    """F1 (#77): the observability log lives under .agent/ and must never be
    staged or committed by sync_commit, which only stages the wiki/ sub-tree."""
    from core.observability import ObservabilityLogger

    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    # The logger writes to .agent/observability.jsonl (outside wiki/).
    obs = ObservabilityLogger(repo_root=str(repo_local_only))
    obs_path = repo_local_only / ".agent" / "observability.jsonl"
    obs_path.write_text('{"event": "search", "keywords": ["secret query"]}\n', encoding="utf-8")
    assert obs_path.exists()  # precondition: the live log target

    # A real wiki write so sync_commit actually produces a commit.
    wiki_proj = repo_local_only / "wiki" / "projects"
    wiki_proj.mkdir(parents=True, exist_ok=True)
    (wiki_proj / "note.md").write_text("note\n", encoding="utf-8")

    assert store.sync_commit(message="wiki note") is True

    # The committed tree contains the wiki note but never the observability log.
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo_local_only), capture_output=True, text=True,
    ).stdout.splitlines()
    assert "wiki/projects/note.md" in tracked
    assert not any("observability.jsonl" in t for t in tracked)
    assert ".agent/observability.jsonl" not in tracked


def test_sync_push_with_remote(repo_with_remote: Path) -> None:
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    # Create a local wiki commit, then push.
    wiki_proj = repo_with_remote / "wiki" / "projects"
    wiki_proj.mkdir(parents=True, exist_ok=True)
    (wiki_proj / "p.md").write_text("p\n", encoding="utf-8")
    store.sync_commit(message="local p")
    store.sync_push()  # should not raise
    assert store._sync_engine.last_push_ts > 0.0


def test_sync_pull_with_remote_branch(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    monkeypatch.setattr("core.sync._git.pull_rebase", lambda *a, **k: called.append(1))
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    store.sync_pull()
    assert called == [1]
    assert store._sync_engine.last_pull_ts > 0.0


def test_success_case_regression_delete_commits_and_pushes_wiki_removal(
    repo_with_remote: Path,
    bare_remote: Path,
    tmp_path: Path,
) -> None:
    """success-case(regression) - synced promotion delete removes old remote file."""
    # given
    store = MemoryStore(repo_root=str(repo_with_remote), sync_config=_sync_config())
    source_path = store.write(
        layer="project",
        item_id="promote-sync",
        content="promote me",
        metadata={"id": "promote-sync", "layer": "project"},
    )
    store.write(
        layer="global",
        item_id="promote-sync",
        content="promote me",
        metadata={"id": "promote-sync", "layer": "global"},
    )

    # when
    store.delete(str(source_path))

    # then
    clone = tmp_path / "remote-checkout"
    subprocess.run(
        ["git", "clone", str(bare_remote), str(clone)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (clone / "wiki" / "global" / "promote-sync.md").exists()
    assert not (clone / "wiki" / "projects" / "promote-sync.md").exists()


# ---------------------------------------------------------------------------
# engine branch coverage
# ---------------------------------------------------------------------------


def test_should_pull_disabled_returns_false(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, SyncConfig(enabled=False))
    assert eng.should_pull() is False


def test_should_pull_autopull_off_returns_false(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config(auto_pull_on_capture=False))
    assert eng.should_pull() is False


def test_should_pull_no_remote_returns_false(repo_local_only: Path) -> None:
    eng = GitSyncEngine(repo_local_only, _sync_config())
    assert eng.should_pull() is False


def test_should_pull_remote_branch_missing(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.sync._git.remote_has_branch", lambda *a, **k: False)
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    assert eng.should_pull() is False


def test_should_pull_remote_has_branch_exception(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("core.sync._git.remote_has_branch", boom)
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    assert eng.should_pull() is False


def test_has_remote_exception_returns_false(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("git missing")

    monkeypatch.setattr("core.sync._git.remote_exists", boom)
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    assert eng.has_remote() is False


def test_hook_before_write_disabled_noop(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.sync._git.pull_rebase",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not pull")),
    )
    eng = GitSyncEngine(repo_with_remote, SyncConfig(enabled=False))
    eng.hook_before_write()  # returns immediately, no pull


def test_hook_after_write_item_disabled(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, SyncConfig(enabled=False))
    assert eng.hook_after_write_item("project", "x", [repo_with_remote / "wiki" / "a.md"]) is False


def test_hook_after_write_item_no_paths(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    assert eng.hook_after_write_item("project", "x", []) is False


def test_hook_after_write_item_all_filtered_out(repo_with_remote: Path) -> None:
    """A stage_filter that rejects everything → no commit."""
    eng = GitSyncEngine(
        repo_with_remote, _sync_config(), stage_filter=lambda paths: []
    )
    p = repo_with_remote / "wiki" / "projects"
    p.mkdir(parents=True, exist_ok=True)
    (p / "z.md").write_text("z\n", encoding="utf-8")
    assert eng.hook_after_write_item("project", "z", [p / "z.md"]) is False


def test_hook_after_commit_disabled(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, SyncConfig(enabled=False))
    eng.hook_after_commit(True)  # no-op


def test_hook_after_commit_not_committed(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.sync._git.push",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not push")),
    )
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    eng.hook_after_commit(False)  # committed=False → skip push


def test_hook_after_commit_auto_push_off(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.sync._git.push",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not push")),
    )
    eng = GitSyncEngine(repo_with_remote, _sync_config(auto_push_after_commit=False))
    eng.hook_after_commit(True)  # auto push disabled → skip


def test_hook_after_commit_no_remote(repo_local_only: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.sync._git.push",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not push")),
    )
    eng = GitSyncEngine(repo_local_only, _sync_config())
    eng.hook_after_commit(True)  # no remote → skip push silently


def test_hook_after_commit_remote_branch_missing(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.sync._git.remote_has_branch", lambda *a, **k: False)
    monkeypatch.setattr(
        "core.sync._git.push",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not push")),
    )
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    eng.hook_after_commit(True)  # branch missing → skip push


def test_hook_after_commit_pushes(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pushed: list[int] = []
    monkeypatch.setattr("core.sync._git.push", lambda *a, **k: pushed.append(1))
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    eng.hook_after_commit(True)
    assert pushed == [1]
    assert eng.last_push_ts > 0.0


def test_sync_pull_remote_branch_missing_skip(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.sync._git.remote_has_branch", lambda *a, **k: False)
    monkeypatch.setattr(
        "core.sync._git.pull_rebase",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not pull")),
    )
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    eng.sync_pull()  # remote exists but branch missing → silent skip


def test_load_last_pull_ts_roundtrip(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    eng._save_last_pull_ts(123.5)
    eng2 = GitSyncEngine(repo_with_remote, _sync_config())
    assert eng2.last_pull_ts == 123.5


def test_save_last_pull_ts_swallows_errors(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config())

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    eng._save_last_pull_ts(1.0)  # must not raise


def test_load_last_pull_ts_missing_returns_zero(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config())
    # Fresh engine on a path whose cache file does not exist.
    assert eng._load_last_pull_ts() == 0.0


def test_hash_path_stable() -> None:
    assert hash_path("/a/b") == hash_path("/a/b")
    assert hash_path("/a/b") != hash_path("/a/c")
    assert len(hash_path("/x")) == 16


def test_explicit_cache_key_used(repo_with_remote: Path) -> None:
    eng = GitSyncEngine(repo_with_remote, _sync_config(), cache_key="custom-key")
    assert eng._pull_cache_path().name == "sync-last-pull-custom-key.ts"


# ---------------------------------------------------------------------------
# sync_continue
# ---------------------------------------------------------------------------


def test_sync_continue_with_conflict_markers_raises(repo_local_only: Path) -> None:
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    proj = repo_local_only / "wiki" / "projects"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "c.md").write_text("<<<<<<< HEAD\nx\n=======\ny\n>>>>>>>\n", encoding="utf-8")
    with pytest.raises(SyncConflictError):
        store.sync_continue()


def test_sync_continue_clean_runs_rebase_and_cleans(
    repo_local_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.sync._git.rebase_continue", lambda *a, **k: None)
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    # Drop a conflict artefact + a transient conflict file to be cleaned up.
    (repo_local_only / "wiki").mkdir(parents=True, exist_ok=True)
    (repo_local_only / "wiki" / "_sync_conflict.md").write_text("conflict\n", encoding="utf-8")
    transient = repo_local_only / "transient"
    transient.mkdir(parents=True, exist_ok=True)
    (transient / "conflict-1.md").write_text("c\n", encoding="utf-8")
    store.sync_continue()
    assert not (repo_local_only / "wiki" / "_sync_conflict.md").exists()
    assert not (transient / "conflict-1.md").exists()
    assert store._sync_engine.last_pull_ts > 0.0


def test_sync_continue_skips_missing_scan_dir(repo_local_only: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan dir that does not exist is skipped without error."""
    monkeypatch.setattr("core.sync._git.rebase_continue", lambda *a, **k: None)
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    # No wiki layer dirs exist yet beyond .gitkeep; sync_continue must run clean.
    store.sync_continue()


def test_sync_continue_unreadable_file_skipped(
    repo_local_only: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.sync._git.rebase_continue", lambda *a, **k: None)
    store = MemoryStore(repo_root=str(repo_local_only), sync_config=_sync_config())
    proj = repo_local_only / "wiki" / "projects"
    proj.mkdir(parents=True, exist_ok=True)
    bad = proj / "bad.md"
    bad.write_text("ok\n", encoding="utf-8")

    real_read = Path.read_text

    def maybe_boom(self, *a, **k):
        if self == bad:
            raise OSError("unreadable")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", maybe_boom)
    store.sync_continue()  # unreadable file skipped, no raise


def test_write_conflict_artefacts_creates_parent(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    eng = GitSyncEngine(
        root, _sync_config(), conflict_filename=str(Path("wiki") / "_sync_conflict.md")
    )
    eng.write_conflict_artefacts("detail here")
    art = root / "wiki" / "_sync_conflict.md"
    assert art.exists()
    assert "detail here" in art.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI — mnemos sync on the DEFAULT backend (issue #69)
# ---------------------------------------------------------------------------


def _write_default_sync_yaml(repo: Path, enabled: bool = True) -> None:
    import yaml
    (repo / "mnemos.yml").write_text(
        yaml.dump(
            {
                "storage": {
                    "backend": "default",
                    "sync": {
                        "enabled": enabled,
                        "remote": "origin",
                        "branch": "main",
                        "pull_rate_limit_seconds": 0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )


def test_cli_sync_status_default_backend(repo_with_remote: Path) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    _write_default_sync_yaml(repo_with_remote)
    result = CliRunner().invoke(
        cli, ["sync", "status"], env={"MNEMOS_REPO_ROOT": str(repo_with_remote)}
    )
    assert result.exit_code == 0, result.output
    assert "sync_enabled" in result.output
    assert "True" in result.output


def test_cli_sync_pull_default_backend(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    monkeypatch.setattr("core.sync._git.pull_rebase", lambda *a, **k: None)
    _write_default_sync_yaml(repo_with_remote)
    result = CliRunner().invoke(
        cli, ["sync", "pull"], env={"MNEMOS_REPO_ROOT": str(repo_with_remote)}
    )
    assert result.exit_code == 0, result.output
    assert "pull complete" in result.output


def test_cli_sync_push_default_backend(repo_with_remote: Path) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    _write_default_sync_yaml(repo_with_remote)
    result = CliRunner().invoke(
        cli, ["sync", "push"], env={"MNEMOS_REPO_ROOT": str(repo_with_remote)}
    )
    assert result.exit_code == 0, result.output
    assert "push complete" in result.output


def test_cli_sync_continue_default_backend(repo_with_remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    monkeypatch.setattr("core.sync._git.rebase_continue", lambda *a, **k: None)
    _write_default_sync_yaml(repo_with_remote)
    result = CliRunner().invoke(
        cli, ["sync", "continue"], env={"MNEMOS_REPO_ROOT": str(repo_with_remote)}
    )
    assert result.exit_code in (0, 1)


def test_cli_sync_rejects_default_backend_without_optin(tmp_path: Path) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    repo = tmp_path / "noopt"
    _init_repo_with_main(repo, bare=None)
    _write_default_sync_yaml(repo, enabled=False)
    result = CliRunner().invoke(
        cli, ["sync", "status"], env={"MNEMOS_REPO_ROOT": str(repo)}
    )
    assert result.exit_code != 0
    assert "default backend" in result.output


def test_cli_sync_init_default_backend(tmp_path: Path, bare_remote: Path) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    repo = tmp_path / "initrepo"
    _init_repo_with_main(repo, bare=None)  # repo exists, no remote yet
    _write_default_sync_yaml(repo)
    result = CliRunner().invoke(
        cli,
        ["sync", "init", "--remote", str(bare_remote)],
        env={"MNEMOS_REPO_ROOT": str(repo)},
    )
    assert result.exit_code == 0, result.output
    assert "init complete" in result.output
    assert git.remote_exists(repo, "origin")


def test_cli_sync_init_rejects_default_backend_without_optin(tmp_path: Path, bare_remote: Path) -> None:
    from click.testing import CliRunner
    from core.cli import cli

    repo = tmp_path / "initnoopt"
    _init_repo_with_main(repo, bare=None)
    _write_default_sync_yaml(repo, enabled=False)
    result = CliRunner().invoke(
        cli,
        ["sync", "init", "--remote", str(bare_remote)],
        env={"MNEMOS_REPO_ROOT": str(repo)},
    )
    assert result.exit_code != 0
    assert "default backend" in result.output
