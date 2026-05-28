"""Shared git-sync engine for mnemos storage backends.

This module factors the git-sync logic that previously lived only inside
:class:`core.obsidian.ObsidianBackend` (issue #24) into a backend-agnostic
:class:`GitSyncEngine` so the **default** :class:`core.store.MemoryStore`
backend can opt into the same remote auto-sync behaviour (issue #69).

Design (DIP / DRY)
------------------
Both backends own a :class:`GitSyncEngine` instance and delegate their sync
hooks and public ``sync_*`` methods to it.  The engine is parameterised so
that backend-specific policy lives in small callbacks / values rather than in
forked copies of the logic:

- ``root`` — the working-tree root the engine operates on (vault path for
  Obsidian, repo root for the default backend).
- ``config`` — a :class:`core.config.SyncConfig` (the master switch + remote /
  branch / rate-limit knobs).
- ``cache_key`` — a stable string used to name the per-root last-pull timestamp
  cache file.  Obsidian passes a hash of the vault path so its cache filename is
  byte-identical to the pre-#69 behaviour.
- ``stage_filter`` — a callback ``(paths) -> list[Path]`` that decides which of
  the changed paths may be staged for commit.  Obsidian passes an identity
  filter (everything).  The default backend passes a filter that keeps only
  paths under ``wiki/`` so a commit never accidentally stages repository source
  code (issue #69 — Decision 2).
- ``conflict_filename`` — the name of the conflict-marker artefact written at
  ``root`` when a pull-rebase fails (``_sync_conflict.md``).
- ``conflict_scan_dirs`` — a callback ``() -> list[Path]`` returning the
  directories whose ``*.md`` files are scanned for unresolved conflict markers
  by :meth:`GitSyncEngine.sync_continue`.

:class:`SyncConflictError` is defined here (it was previously defined in
``core.obsidian``); ``core.obsidian`` re-exports it for backward compatibility.
"""
from __future__ import annotations

import datetime
import hashlib
import time
from pathlib import Path
from typing import Callable, Sequence

import core.git as _git
from core.config import SyncConfig


# ---------------------------------------------------------------------------
# Sync-specific exceptions
# ---------------------------------------------------------------------------


class SyncConflictError(RuntimeError):
    """Raised when ``git pull --rebase`` encounters an unresolvable conflict.

    The caller should surface this as ``STATUS: blocked`` and instruct the
    user to run ``mnemos sync continue`` after manually resolving.
    """

    def __init__(self, message: str, conflicting_ids: list[str] | None = None) -> None:
        self.conflicting_ids = conflicting_ids or []
        super().__init__(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_path(path: Path | str) -> str:
    """Return a short stable hash of *path* for per-root cache file names."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 ``...Z`` string."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Default conflict-artefact filename (mirrors the Obsidian behaviour).
DEFAULT_CONFLICT_FILENAME = "_sync_conflict.md"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class GitSyncEngine:
    """Backend-agnostic git-sync engine.

    See the module docstring for the parameter contract.  An instance is
    *inert* when ``config.enabled`` is ``False`` — every hook returns early and
    no git command runs, so a backend that constructs an engine with a disabled
    config behaves exactly as it did before sync existed.
    """

    def __init__(
        self,
        root: str | Path,
        config: SyncConfig,
        *,
        cache_key: str | None = None,
        stage_filter: Callable[[Sequence[Path]], list[Path]] | None = None,
        conflict_filename: str = DEFAULT_CONFLICT_FILENAME,
        conflict_scan_dirs: Callable[[], list[Path]] | None = None,
    ) -> None:
        self._root = Path(root)
        self._config = config
        self._cache_key = cache_key or hash_path(self._root)
        # Identity filter by default (stage everything that changed).
        self._stage_filter = stage_filter or (lambda paths: list(paths))
        self._conflict_filename = conflict_filename
        # Default scan: the root itself (for backends without layer sub-dirs).
        self._conflict_scan_dirs = conflict_scan_dirs or (lambda: [self._root])

        # Monotonic timestamp (epoch float) of the last successful pull,
        # persisted across process restarts via a per-root cache file.
        self._last_pull_ts: float = self._load_last_pull_ts()
        # Monotonic timestamp of the last successful push (process-local).
        self._last_push_ts: float = 0.0

    # ------------------------------------------------------------------ #
    # Read-only accessors (used by sync_status)                           #
    # ------------------------------------------------------------------ #

    @property
    def last_pull_ts(self) -> float:
        return self._last_pull_ts

    @property
    def last_push_ts(self) -> float:
        return self._last_push_ts

    # ------------------------------------------------------------------ #
    # Pull-timestamp cache                                                #
    # ------------------------------------------------------------------ #

    def _pull_cache_path(self) -> Path:
        """Return the path to the per-root last-pull timestamp cache file."""
        cache_dir = Path.home() / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"sync-last-pull-{self._cache_key}.ts"

    def _load_last_pull_ts(self) -> float:
        """Load the last-pull timestamp from disk.  Returns 0.0 if absent."""
        try:
            return float(self._pull_cache_path().read_text(encoding="utf-8").strip())
        except Exception:
            return 0.0

    def _save_last_pull_ts(self, ts: float) -> None:
        """Persist the last-pull timestamp to disk (best-effort)."""
        try:
            self._pull_cache_path().write_text(str(ts), encoding="utf-8")
        except Exception:
            pass  # non-fatal — the in-memory value is still correct

    # ------------------------------------------------------------------ #
    # Remote / rate-limit helpers                                         #
    # ------------------------------------------------------------------ #

    def has_remote(self) -> bool:
        """Return ``True`` when the configured remote exists in the repo.

        Returns ``False`` on any error (git not found, not a repo, etc.) so
        callers can treat the absence of a remote as local-only mode.
        """
        try:
            return _git.remote_exists(self._root, self._config.remote)
        except Exception:
            return False

    def should_pull(self) -> bool:
        """Return ``True`` when a pull should be attempted now.

        A pull is skipped when sync/auto-pull is disabled, the remote does not
        exist (local-only), the remote branch is missing (empty remote), or the
        rate-limit window has not elapsed.
        """
        if not self._config.enabled or not self._config.auto_pull_on_capture:
            return False
        if not self.has_remote():
            return False
        try:
            if not _git.remote_has_branch(
                self._root, self._config.remote, self._config.branch
            ):
                return False
        except Exception:
            return False
        elapsed = time.monotonic() - self._last_pull_ts
        return elapsed >= self._config.pull_rate_limit_seconds

    # ------------------------------------------------------------------ #
    # Write hooks                                                         #
    # ------------------------------------------------------------------ #

    def _do_pull_rebase(self) -> None:
        """Run the actual pull-rebase, updating the pull timestamp on success.

        On a git conflict, writes the conflict artefact and raises
        :exc:`SyncConflictError`.  This is the low-level primitive used by both
        :meth:`hook_before_write` and :meth:`sync_pull`; backends that need a
        custom rate-limit decision (see :class:`core.obsidian.ObsidianBackend`)
        call it directly after their own ``should_pull`` gate.
        """
        try:
            _git.pull_rebase(
                self._root,
                remote=self._config.remote,
                branch=self._config.branch,
                autostash=True,
            )
            now = time.monotonic()
            self._last_pull_ts = now
            self._save_last_pull_ts(now)
        except _git.GitCommandError as exc:
            self.write_conflict_artefacts(str(exc))
            raise SyncConflictError(
                f"git pull (fetch+rebase) failed: {exc}",
            ) from exc

    def hook_before_write(self) -> None:
        """Hook 1 — attempt a rate-limited pull-rebase before a disk write.

        On pull failure (conflict) writes the conflict artefact and raises
        :exc:`SyncConflictError`.
        """
        if not self._config.enabled:
            return
        if not self.should_pull():
            return
        self._do_pull_rebase()

    def write_conflict_artefacts(self, error_detail: str) -> None:
        """Write the conflict-notice artefact at the configured root."""
        conflict_path = self._root / self._conflict_filename
        lines = [
            "# mnemos Sync Conflict",
            "",
            "_A ``git fetch`` + ``git rebase`` conflict was detected._",
            "",
            "## What Happened",
            "",
            "mnemos attempted to pull the latest changes from the remote but",
            "encountered a conflict that could not be resolved automatically.",
            "",
            "## Error Detail",
            "",
            "```",
            error_detail,
            "```",
            "",
            "## How to Resolve",
            "",
            "1. Open this repository in your terminal.",
            "2. Resolve the conflict markers in the affected ``.md`` files.",
            "3. Run ``git add <resolved-files>``.",
            "4. Run ``mnemos sync continue`` (or ``git rebase --continue``).",
            "5. Delete this file after the conflict is resolved.",
            "",
        ]
        conflict_path.parent.mkdir(parents=True, exist_ok=True)
        conflict_path.write_text("\n".join(lines), encoding="utf-8")

    def hook_after_write_item(
        self,
        layer: str,
        item_id: str,
        changed_paths: Sequence[Path],
    ) -> bool:
        """Hook 2 (item-level) — stage and commit changed paths.

        Only paths accepted by ``stage_filter`` are staged, so a backend that
        restricts commits to a sub-tree (e.g. ``wiki/``) never stages anything
        outside it.  Returns ``True`` if a commit was actually created.
        """
        if not self._config.enabled:
            return False
        if not changed_paths:
            return False
        stageable = self._stage_filter(list(changed_paths))
        if not stageable:
            # All changed paths were filtered out (e.g. a non-wiki write on the
            # default backend) — nothing to commit.
            return False
        _git.add(self._root, [str(p) for p in stageable])
        message = self._config.commit_message_template.format(
            layer=layer,
            id=item_id,
            timestamp=_utc_timestamp(),
        )
        return _git.commit(self._root, message)

    def hook_after_commit(self, committed: bool) -> None:
        """Hook 3 — push after a successful commit.

        Runs only when ``committed`` AND ``auto_push_after_commit`` AND a remote
        with the target branch already exists.  Otherwise the push is skipped
        silently (local-only mode / empty remote).
        """
        if not self._config.enabled:
            return
        if not committed:
            return
        if not self._config.auto_push_after_commit:
            return
        if not self.has_remote():
            return
        if not _git.remote_has_branch(
            self._root, self._config.remote, self._config.branch
        ):
            return
        _git.push(self._root, remote=self._config.remote, branch=self._config.branch)
        self._last_push_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    # Public sync API                                                     #
    # ------------------------------------------------------------------ #

    def sync_pull(self) -> None:
        """Manual pull — bypasses the rate limit but honours local-only mode.

        Returns silently when no remote is configured or the remote branch does
        not yet exist.  Raises :exc:`SyncConflictError` on a pull conflict.
        """
        if not self.has_remote():
            return
        if not _git.remote_has_branch(
            self._root, self._config.remote, self._config.branch
        ):
            return
        self._do_pull_rebase()

    def sync_push(self) -> None:
        """Manual push to the configured remote/branch.

        Returns silently in local-only mode.  Raises
        :exc:`core.git.GitCommandError` on push failure.
        """
        if not self.has_remote():
            return
        _git.push(self._root, remote=self._config.remote, branch=self._config.branch)
        self._last_push_ts = time.monotonic()

    def sync_commit(self, message: str | None = None) -> bool:
        """Stage the backend's committable paths and create a commit.

        The set of staged paths is decided by ``stage_filter`` applied to the
        engine root: identity for Obsidian (effectively ``git add .``), and the
        ``wiki/``-scoped filter for the default backend.  Returns ``True`` when
        a commit was created.
        """
        stageable = self._stage_filter([self._root])
        if stageable:
            _git.add(self._root, [str(p) for p in stageable])
        if message is None:
            message = f"mnemos: manual sync commit ({_utc_timestamp()})"
        return _git.commit(self._root, message)

    def sync_status(self) -> dict[str, object]:
        """Return a dict describing the current sync state.

        Mirrors :func:`core.git.status` and adds ``last_pull_ts``,
        ``last_push_ts``, ``sync_enabled``, ``sync_remote``, ``sync_branch``.
        """
        stat = _git.status(self._root)
        stat["last_pull_ts"] = self._last_pull_ts
        stat["last_push_ts"] = self._last_push_ts
        stat["sync_enabled"] = self._config.enabled
        stat["sync_remote"] = self._config.remote
        stat["sync_branch"] = self._config.branch
        return stat

    def sync_continue(self) -> None:
        """Continue after a manually-resolved rebase conflict.

        1. Verify no conflict markers remain in any scanned ``.md`` file.
        2. ``git rebase --continue``.
        3. Delete the conflict artefact and ``transient/conflict-*.md``.

        Raises :exc:`SyncConflictError` when conflict markers (``<<<<<<<``)
        are still present.
        """
        conflicted: list[str] = []
        for scan_dir in self._conflict_scan_dirs():
            if not scan_dir.exists():
                continue
            for md_file in scan_dir.glob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "<<<<<<<" in text:
                    conflicted.append(str(md_file))

        if conflicted:
            raise SyncConflictError(
                "Conflict markers still present in repository files. "
                "Resolve all conflicts before running 'mnemos sync continue'.\n"
                + "\n".join(conflicted)
            )

        _git.rebase_continue(self._root)

        conflict_index = self._root / self._conflict_filename
        if conflict_index.exists():
            conflict_index.unlink()

        transient_dir = self._root / "transient"
        if transient_dir.exists():
            for f in transient_dir.glob("conflict-*.md"):
                f.unlink()

        now = time.monotonic()
        self._last_pull_ts = now
        self._save_last_pull_ts(now)
