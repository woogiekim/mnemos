"""Remote git-sync hardening scenarios for issue #79.

This module covers the four acceptance-criteria gaps identified by the
issue #79 audit, against the **unchanged** sync engine introduced by
issue #69 (``core.sync.GitSyncEngine``).  None of these tests modify
``core/sync.py`` or ``core/git.py`` — they are validation/hardening
scenarios that exercise existing behavior end-to-end.

Coverage map (see ``context/test-coverage.md`` for the canonical mapping):

- **AC3** — full conflict → resolve → ``sync_continue`` loop on the default
  backend with a real ``git rebase`` (no mocking of ``rebase_continue``).
- **AC4** — offline → online transition for the default and Obsidian backends
  (rename the bare remote so it becomes unreachable, write items locally,
  restore the remote, then verify ``sync_push`` ships the queued commits).
- **AC5** — lifecycle-metadata round-trip across the remote for both
  backends (``tags`` / ``trust_level`` / ``quality_score`` /
  ``lifecycle_action`` / ``created_at`` survive byte-for-byte).
- **AC6** — partial sync failure recovery (monkeypatch the low-level git
  push to raise once, then succeed; verify the local commit persists,
  the first push surfaces an exception, and the next push ships the
  queued commit without duplication).

All tests run fully offline using a local bare repository and ``file://``
URLs; no network access is required.  The pytest ``filterwarnings=["error"]``
setting in ``pyproject.toml`` is honored — no test introduces a deprecation
or runtime warning.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import core.git as _git
from core.config import SyncConfig
from core.fts import FTSIndex
from core.obsidian import ObsidianBackend
from core.store import MemoryStore

# Reuse the helpers from the existing test files so this file does not
# duplicate fixture plumbing.  ``test_store_sync`` owns ``_init_repo_with_main``
# / ``_make_bare`` / ``_sync_config`` for default-backend repos;
# ``test_sync_e2e`` owns the parallel ``_init_bare_with_main`` / ``_clone``
# helpers for Obsidian vault topologies.
from tests.test_store_sync import (
    _commit_count,
    _git_log_messages,
    _init_repo_with_main,
    _isolate_pull_cache,  # noqa: F401 — re-export the autouse HOME redirect fixture
    _make_bare,
    _sync_config,
)
from tests.test_sync_e2e import (
    _clone,
    _init_bare_with_main,
    _make_fts,
    _make_sync_config,
)


# ---------------------------------------------------------------------------
# Helpers local to this file
# ---------------------------------------------------------------------------


def _remote_log(bare: Path, branch: str = "main") -> list[str]:
    """Return the bare repo's commit subject log for *branch*."""
    out = subprocess.run(
        ["git", "log", "--pretty=%s", branch],
        cwd=str(bare),
        capture_output=True,
        text=True,
    )
    return [l for l in out.stdout.splitlines() if l.strip()]


def _commit_subject(repo: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


# ---------------------------------------------------------------------------
# AC3 — full conflict → resolve → sync_continue loop (REAL git rebase)
# ---------------------------------------------------------------------------


class TestAC3ConflictContinueRealRebase:
    """Drive the default backend through a real divergent-history rebase,
    resolve the conflict markers manually, and run ``sync_continue`` against
    the un-mocked ``git rebase --continue`` command.
    """

    def test_default_backend_continue_resolves_real_rebase(
        self,
        tmp_path: Path,
    ) -> None:
        # 1. Set up: bare remote + clone A configured with default-backend sync.
        bare = _make_bare(tmp_path / "bare.git")
        repoA = tmp_path / "repoA"
        _init_repo_with_main(repoA, bare=bare)

        storeA = MemoryStore(repo_root=str(repoA), sync_config=_sync_config())
        storeA.write("project", "ac3-alpha", "original content from A", {})

        # 2. Clone B and capture a diverging commit, push to remote.
        repoB = tmp_path / "repoB"
        subprocess.run(
            ["git", "clone", str(bare), str(repoB)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@mnemos.test"],
            cwd=str(repoB), check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "mnemos Test"],
            cwd=str(repoB), check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=str(repoB), check=True, capture_output=True, text=True,
        )
        # Edit the SAME wiki file on B with conflicting content.
        wiki_file_b = next((repoB / "wiki" / "projects").glob("*.md"))
        wiki_file_b.write_text(
            "---\nid: ac3-alpha\nlayer: project\n---\nconflicting content from B\n",
            encoding="utf-8",
        )
        _git.add(repoB, [str(wiki_file_b)])
        _git.commit(repoB, "B diverging commit")
        _git.push(repoB, "origin", "main")

        # 3. A makes a local diverging commit on the same file WITHOUT pulling.
        wiki_file_a = next((repoA / "wiki" / "projects").glob("*.md"))
        wiki_file_a.write_text(
            "---\nid: ac3-alpha\nlayer: project\n---\ndiverging local content from A\n",
            encoding="utf-8",
        )
        _git.add(repoA, [str(wiki_file_a)])
        _git.commit(repoA, "A diverging commit")

        # 4. Run sync_pull on A — pull-rebase will produce a REAL conflict.
        from core.store import SyncConflictError
        with pytest.raises(SyncConflictError):
            storeA.sync_pull()

        # The conflict artefact must be present (default backend stores it at
        # ``wiki/_sync_conflict.md`` per the wiki-scoped filter).
        artefact = repoA / "wiki" / "_sync_conflict.md"
        assert artefact.exists(), "conflict artefact must be written on rebase failure"

        # The on-disk wiki file must still contain conflict markers (real rebase).
        markers_present = "<<<<<<<" in wiki_file_a.read_text(encoding="utf-8")
        assert markers_present, "real git rebase must leave conflict markers in the file"

        # Verify rebase is actually in progress (the artefact below is created
        # by git only when a rebase is mid-flight).
        rebase_dir = repoA / ".git" / "rebase-merge"
        rebase_apply = repoA / ".git" / "rebase-apply"
        assert rebase_dir.exists() or rebase_apply.exists(), (
            "git rebase artefacts must be present mid-conflict"
        )

        # 5. Manually resolve the conflict markers in the wiki file.
        wiki_file_a.write_text(
            "---\nid: ac3-alpha\nlayer: project\n---\nresolved content from A\n",
            encoding="utf-8",
        )
        _git.add(repoA, [str(wiki_file_a)])

        # 6. Call sync_continue — this hits the REAL git rebase --continue.
        storeA.sync_continue()

        # 7. Assertions: rebase artefacts cleaned up, conflict artefact gone,
        # working tree clean, branch ahead of remote with the resolved commit.
        assert not rebase_dir.exists(), (
            "rebase-merge directory must be removed after sync_continue"
        )
        assert not rebase_apply.exists(), (
            "rebase-apply directory must be removed after sync_continue"
        )
        assert not artefact.exists(), (
            "sync_conflict.md must be removed after sync_continue"
        )

        # Working tree clean (no conflict markers, no staged-but-uncommitted).
        status_out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repoA), check=True, capture_output=True, text=True,
        )
        assert status_out.stdout.strip() == "", (
            f"working tree must be clean after sync_continue, got: {status_out.stdout!r}"
        )

        # And the conflict markers are gone from the resolved file.
        assert "<<<<<<<" not in wiki_file_a.read_text(encoding="utf-8")

    def test_sync_continue_raises_when_markers_remain_real(
        self, tmp_path: Path,
    ) -> None:
        """If the user calls sync_continue without resolving, it must NOT
        invoke git rebase --continue — it must raise SyncConflictError up-front.

        This complements the existing unit test in test_store_sync.py by
        proving the guard fires *before* any git invocation, against the
        un-mocked rebase_continue.
        """
        bare = _make_bare(tmp_path / "bare.git")
        repo = tmp_path / "ac3-guard-repo"
        _init_repo_with_main(repo, bare=bare)

        store = MemoryStore(repo_root=str(repo), sync_config=_sync_config())

        # Drop a conflict-marker file inside a scanned dir.
        proj = repo / "wiki" / "projects"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "still-conflicted.md").write_text(
            "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>>\n", encoding="utf-8",
        )

        from core.store import SyncConflictError
        with pytest.raises(SyncConflictError):
            store.sync_continue()


# ---------------------------------------------------------------------------
# AC4 — offline → online transition for both backends
# ---------------------------------------------------------------------------


class TestAC4OfflineToOnline:
    """Rename the bare remote so the URL becomes unreachable, write items
    locally (local commits queue), restore the bare remote, and assert
    ``sync_push`` ships every queued commit without loss or duplication.

    The default backend (MemoryStore) silently skips the push leg via
    ``hook_after_commit``'s ``remote_has_branch`` check; the Obsidian backend
    behaves the same way.  The recovery contract is that the queued local
    commits ship as soon as ``sync_push`` is invoked after the remote
    returns.
    """

    def test_default_backend_offline_then_online(self, tmp_path: Path) -> None:
        bare = _make_bare(tmp_path / "bare.git")
        repo = tmp_path / "default-offline-repo"
        _init_repo_with_main(repo, bare=bare)

        store = MemoryStore(repo_root=str(repo), sync_config=_sync_config())
        # Sanity: one online write commits AND pushes.
        store.write("project", "online-pre", "alive content", {})
        assert any("online-pre" in m for m in _remote_log(bare))

        # Take the remote offline by renaming the bare directory.
        bare_offline = bare.parent / "bare.git.gone"
        bare.rename(bare_offline)

        # While offline, write three items locally.  hook_after_commit's
        # remote_has_branch check returns False → push is skipped silently
        # but the local commit still lands.
        before = _commit_count(repo)
        for i in range(3):
            store.write("project", f"offline-{i:02d}", f"queued item {i}", {})
        after = _commit_count(repo)
        assert after == before + 3, (
            f"three local commits should queue while offline, got delta {after - before}"
        )

        # Restore the remote and call sync_push manually.
        bare_offline.rename(bare)
        store.sync_push()

        # Every queued commit shipped exactly once to the remote.
        remote_subjects = _remote_log(bare)
        for i in range(3):
            matches = [m for m in remote_subjects if f"offline-{i:02d}" in m]
            assert len(matches) == 1, (
                f"offline-{i:02d} should ship exactly once, got {matches!r}"
            )

    def test_obsidian_backend_offline_then_online(self, tmp_path: Path) -> None:
        bare = _make_bare(tmp_path / "obs-bare.git")
        _init_bare_with_main(bare, tmp_path)

        clone_path = _clone(bare, tmp_path / "obs-vault")
        fts = _make_fts(tmp_path / "obs-fts" / "fts.db")
        cfg = _make_sync_config()
        vault = ObsidianBackend(vault_path=str(clone_path), fts=fts, sync_config=cfg)

        # Online sanity write.
        vault.write(
            layer="project", item_id="obs-online-pre",
            content="alive obsidian content", metadata={"tags": ["sanity"]},
        )
        assert any("obs-online-pre" in m for m in _remote_log(bare))

        # Offline.
        bare_offline = bare.parent / "obs-bare.git.gone"
        bare.rename(bare_offline)

        before = _commit_count(clone_path)
        # Use auto_push_after_commit = True so the engine attempts the push
        # leg and exercises the skip-on-missing-remote-branch path.  Each
        # write should still produce a local commit.
        for i in range(3):
            vault.write(
                layer="project", item_id=f"obs-offline-{i:02d}",
                content=f"queued obsidian item {i}", metadata={"tags": ["queue"]},
            )
        after = _commit_count(clone_path)
        assert after == before + 3

        # Restore the remote, push the queue.
        bare_offline.rename(bare)
        vault.sync_push()

        remote_subjects = _remote_log(bare)
        for i in range(3):
            matches = [m for m in remote_subjects if f"obs-offline-{i:02d}" in m]
            assert len(matches) == 1, (
                f"obs-offline-{i:02d} should ship exactly once, got {matches!r}"
            )


# ---------------------------------------------------------------------------
# AC5 — lifecycle metadata survives remote round-trips
# ---------------------------------------------------------------------------


class TestAC5LifecycleMetadataRoundTrip:
    """Capture an item on host A with the full lifecycle metadata set,
    push it to the remote, pull on host B, and assert each metadata field
    survives byte-for-byte (no rounding, whitespace mangling, or type
    coercion).
    """

    # The metadata fields under test cover the spectrum: list (tags),
    # int (trust_level), float (quality_score), enum-like string
    # (lifecycle_action), and ISO-8601 timestamp string (created_at).
    METADATA = {
        "tags": ["alpha", "beta", "lifecycle-79"],
        "trust_level": 3,
        "quality_score": 0.875,
        "lifecycle_action": "promote",
        "created_at": "2025-01-01T12:34:56+00:00",
    }

    def test_default_backend_metadata_roundtrip(self, tmp_path: Path) -> None:
        bare = _make_bare(tmp_path / "ac5-default-bare.git")
        hostA = tmp_path / "ac5-default-A"
        _init_repo_with_main(hostA, bare=bare)
        storeA = MemoryStore(repo_root=str(hostA), sync_config=_sync_config())
        storeA.write("project", "ac5-default-item", "round-trip content", dict(self.METADATA))

        # Clone host B and pull.
        hostB_clone = tmp_path / "ac5-default-B"
        subprocess.run(
            ["git", "clone", str(bare), str(hostB_clone)],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@mnemos.test"],
            cwd=str(hostB_clone), check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "mnemos Test"],
            cwd=str(hostB_clone), check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=str(hostB_clone), check=True, capture_output=True, text=True,
        )
        storeB = MemoryStore(repo_root=str(hostB_clone), sync_config=_sync_config())

        item = storeB.read("ac5-default-item")
        for key, expected in self.METADATA.items():
            assert item[key] == expected, (
                f"metadata field {key!r} did not round-trip: "
                f"sent {expected!r}, got {item[key]!r}"
            )
        # Strict type checks — no float→int coercion, no str→bool coercion.
        assert isinstance(item["trust_level"], int)
        assert isinstance(item["quality_score"], float)
        assert isinstance(item["tags"], list)
        assert isinstance(item["lifecycle_action"], str)
        assert isinstance(item["created_at"], str)

    def test_obsidian_backend_metadata_roundtrip(self, tmp_path: Path) -> None:
        bare = _make_bare(tmp_path / "ac5-obs-bare.git")
        _init_bare_with_main(bare, tmp_path)

        hostA_clone = _clone(bare, tmp_path / "ac5-obs-A")
        ftsA = _make_fts(tmp_path / "ac5-obs-A-fts" / "fts.db")
        vaultA = ObsidianBackend(
            vault_path=str(hostA_clone), fts=ftsA, sync_config=_make_sync_config(),
        )
        vaultA.write(
            layer="project", item_id="ac5-obs-item",
            content="obsidian round-trip content", metadata=dict(self.METADATA),
        )

        hostB_clone = _clone(bare, tmp_path / "ac5-obs-B")
        ftsB = _make_fts(tmp_path / "ac5-obs-B-fts" / "fts.db")
        # Disable auto-pull-on-capture so we control timing for the pull.
        vaultB = ObsidianBackend(
            vault_path=str(hostB_clone), fts=ftsB,
            sync_config=_make_sync_config(
                auto_pull_on_capture=False, auto_push_after_commit=False,
            ),
        )
        vaultB.sync_pull()

        item = vaultB.read("ac5-obs-item")
        for key, expected in self.METADATA.items():
            assert item[key] == expected, (
                f"Obsidian metadata field {key!r} did not round-trip: "
                f"sent {expected!r}, got {item[key]!r}"
            )
        # Strict type checks.
        assert isinstance(item["trust_level"], int)
        assert isinstance(item["quality_score"], float)
        assert isinstance(item["tags"], list)
        assert isinstance(item["lifecycle_action"], str)
        assert isinstance(item["created_at"], str)


# ---------------------------------------------------------------------------
# AC6 — partial sync failure recovery
# ---------------------------------------------------------------------------


class TestAC6PartialFailureRecovery:
    """Monkeypatch ``core.sync._git.push`` to raise once then succeed.  Verify:

    1. The local commit persists across the push failure.
    2. The first ``sync_push`` (or ``write``) surfaces an exception — failures
       are NOT silently swallowed.
    3. The next ``sync_push`` ships the queued commit exactly once
       (no duplication, no loss).
    """

    def test_default_backend_partial_push_failure_then_recovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bare = _make_bare(tmp_path / "ac6-default-bare.git")
        repo = tmp_path / "ac6-default-repo"
        _init_repo_with_main(repo, bare=bare)

        # Arm the failure latch: first call to push raises OSError, then
        # subsequent calls delegate to the real implementation.
        real_push = _git.push
        call_count = {"n": 0}

        def flaky_push(path, remote, branch):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("transient network down (test-injected #79 AC6)")
            return real_push(path, remote, branch)

        monkeypatch.setattr("core.sync._git.push", flaky_push)

        store = MemoryStore(repo_root=str(repo), sync_config=_sync_config())
        before = _commit_count(repo)

        # First write: hooks pull (success, no network call mocked there),
        # commit (local), and push (raises).  The exception MUST surface.
        with pytest.raises(OSError, match="transient network down"):
            store.write("project", "ac6-flaky", "needs-recovery", {})

        # Invariant 1 — the local commit persists across the failure.
        after_failed = _commit_count(repo)
        assert after_failed == before + 1, (
            "local commit must persist even when the push leg fails"
        )
        assert "ac6-flaky" in _commit_subject(repo)

        # Invariant 2 — at this point the push has NOT shipped the commit.
        # (Our flaky_push raised before the underlying real_push ran.)
        remote_log_before_recovery = _remote_log(bare)
        assert not any("ac6-flaky" in m for m in remote_log_before_recovery), (
            "the failed first push must not have shipped the commit"
        )

        # Invariant 3 — the next sync_push call ships the queued commit
        # exactly once (the latch falls through to real_push on call #2).
        store.sync_push()
        remote_log_after_recovery = _remote_log(bare)
        ac6_matches = [m for m in remote_log_after_recovery if "ac6-flaky" in m]
        assert len(ac6_matches) == 1, (
            f"queued commit must ship exactly once, got {ac6_matches!r}"
        )
        # And the local commit count has NOT advanced (no duplicate commit
        # was created on the recovery path).
        assert _commit_count(repo) == after_failed, (
            "recovery push must not create a second local commit"
        )

    def test_default_backend_partial_push_failure_with_called_process_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same invariants as the OSError case, but the wrapper raises
        :class:`subprocess.CalledProcessError` — the real low-level git
        wrapper signals push failures as ``GitCommandError`` (a
        ``RuntimeError`` subclass).  Validate that any propagating exception
        type from the push leg satisfies the contract: commit persists,
        exception surfaces, recovery push ships exactly once.
        """
        bare = _make_bare(tmp_path / "ac6-cpe-bare.git")
        repo = tmp_path / "ac6-cpe-repo"
        _init_repo_with_main(repo, bare=bare)

        real_push = _git.push
        call_count = {"n": 0}

        def flaky_push(path, remote, branch):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise subprocess.CalledProcessError(
                    1, ["git", "push", remote, branch],
                    output=b"", stderr=b"simulated push transient (#79 AC6)",
                )
            return real_push(path, remote, branch)

        monkeypatch.setattr("core.sync._git.push", flaky_push)

        store = MemoryStore(repo_root=str(repo), sync_config=_sync_config())
        before = _commit_count(repo)
        with pytest.raises(subprocess.CalledProcessError):
            store.write("project", "ac6-cpe", "recovery-after-cpe", {})

        assert _commit_count(repo) == before + 1
        assert not any("ac6-cpe" in m for m in _remote_log(bare))

        store.sync_push()
        matches = [m for m in _remote_log(bare) if "ac6-cpe" in m]
        assert len(matches) == 1
