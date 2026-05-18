"""Obsidian vault storage backend for mnemos.

Implements the :class:`~core.store.StorageBackend` Protocol introduced in
PR-A (commit ee60c35).  Items are stored as Markdown files with YAML
front-matter inside an Obsidian vault, organised by layer into sub-folders:

    <vault>/session/<id>.md
    <vault>/project/<id>.md
    <vault>/global/<id>.md
    <vault>/transient/<id>.md
    <vault>/ephemeral/<id>.md
    <vault>/working/<id>.md
    <vault>/entities/<id>.md
    <vault>/claims/<id>.md
    <vault>/topics/<id>.md

**Layer promotion** is implemented as an atomic file-move from the source
folder to the target folder, plus a front-matter ``layer`` field update.

**Bidirectional editing**: ``sync_edits()`` scans vault files for
mtime-based changes, recomputes ``content_hash``, and reconciles the
front-matter — preserving the user's edited content while keeping mnemos
metadata consistent.

**FTS integration**: Every write / update / delete is mirrored to the
shared :class:`~core.fts.FTSIndex` so full-text search continues to work
across backends.

**Wiki-link cross-references**: The existing :class:`~core.store.MemoryStore`
does not embed internal cross-references in item *content*, so there are no
pointer-translation concerns for migrated items.  New items captured
directly via the Obsidian backend may use ``[[<id>]]`` syntax in their
content body for native Obsidian Graph integration.

**Auto-sync** (issue #25): When ``storage.backend: obsidian`` and
``vault_path`` is set in ``mnemos.yml``, sync is **enabled automatically**
without requiring ``sync.enabled: true``.  To opt out, set
``sync.enabled: false`` explicitly.

**Multi-host sync** (issue #24): When sync is enabled (auto or explicit),
:class:`ObsidianBackend` performs automatic git pull/commit/push around every
write operation.  The three hook points are:

1. **Before write** — pull rebase (rate-limited) to get remote changes.
2. **After write** — commit the changed file(s).
3. **After commit** — push to the configured remote (if ``auto_push_after_commit``).

**Local-only mode**: When sync is enabled but no git remote is configured in
the vault, pull and push operations are skipped silently.  The vault still
receives local git commits on every write, so the history is preserved.
Connecting a remote later (via ``git remote add origin <url>``) immediately
activates full bidirectional sync without any configuration change.

When ``sync.enabled: false`` (explicit opt-out) none of these hooks run and the
backend behaves exactly as it did before #24.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterator

import frontmatter

from core.fts import FTSIndex


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
# Constants
# ---------------------------------------------------------------------------

#: All layers mnemos recognises — maps each to a vault sub-folder name.
#: The folder name is the same as the layer name, matching the Q3 decision.
OBSIDIAN_LAYERS: list[str] = [
    "session",
    "project",
    "global",
    "transient",
    "ephemeral",
    "working",
    "entities",
    "claims",
    "topics",
]

#: Quality score threshold below which an item is flagged as low-quality.
LOW_QUALITY_THRESHOLD: float = 0.3


# ---------------------------------------------------------------------------
# Internal helpers (content hash)
# ---------------------------------------------------------------------------


def _nfkc_normalise(content: str) -> str:
    """Apply NFKC normalisation + whitespace collapse + lowercase."""
    nfkc = unicodedata.normalize("NFKC", content)
    return re.sub(r"\s+", " ", nfkc.strip()).lower()


def _content_hash(content: str) -> str:
    """Return SHA-256 hex digest of the NFKC-normalised *content*.

    Matches ``core.gateway._capture_content_hash`` exactly so hashes are
    consistent across backends.
    """
    return hashlib.sha256(_nfkc_normalise(content).encode("utf-8")).hexdigest()


def _content_slug(content: str, max_words: int = 6, max_chars: int = 60) -> str:
    """Derive a human-readable slug from the first line of *content*.

    Keeps Unicode letters and digits (including Korean, CJK, etc.);
    replaces everything else with hyphens.  Falls back to ``"untitled"``
    when the content yields no usable words.
    """
    first_line = content.strip().split("\n")[0][:200]
    cleaned = ""
    for ch in first_line:
        cat = unicodedata.category(ch)
        if cat.startswith(("L", "N")):  # Letters (any script) and Numbers
            cleaned += ch
        else:
            cleaned += " "
    cleaned = re.sub(r"\s+", " ", cleaned).strip().lower()
    words = cleaned.split()[:max_words]
    if not words:
        return "untitled"
    slug = "-".join(words)
    if len(slug) > max_chars:
        slug = slug[:max_chars].rstrip("-")
    return slug or "untitled"


# ---------------------------------------------------------------------------
# ObsidianBackend
# ---------------------------------------------------------------------------


class ObsidianBackend:
    """Obsidian-vault implementation of the :class:`~core.store.StorageBackend` Protocol.

    :param vault_path: Absolute path to the Obsidian vault directory.
    :param fts: Optional :class:`~core.fts.FTSIndex` instance.  When
        supplied, every write / update / delete is also indexed so that
        full-text search results are consistent regardless of which backend
        is active.
    :param sync_config: Optional :class:`~core.config.SyncConfig` instance.
        When ``None`` (the default) sync is disabled.  Pass a
        ``SyncConfig(enabled=True, ...)`` to enable automatic git hooks.
    """

    def __init__(
        self,
        vault_path: str,
        fts: FTSIndex | None = None,
        sync_config: "SyncConfig | None" = None,  # type: ignore[name-defined]
    ) -> None:
        self._vault = Path(vault_path)
        self._vault.mkdir(parents=True, exist_ok=True)
        self._fts: FTSIndex | None = fts
        # mtime cache: maps str(path) → float (mtime at last sync)
        self._mtime_cache: dict[str, float] = {}

        # ── Sync wiring (issue #24) ────────────────────────────────────────
        # Import lazily so the module loads even when core/git.py is absent
        # (defensive import — the file is always present after #24 but belt-
        # and-suspenders is cheap).
        from core.config import SyncConfig as _SyncConfig  # noqa: F401
        self._sync: "_SyncConfig" = sync_config or _SyncConfig()
        # Monotonic timestamp (epoch float) of the last successful pull.
        # Persisted to / loaded from a per-vault cache file on disk so the
        # rate-limit survives process restarts.
        self._last_pull_ts: float = self._load_last_pull_ts()
        # Monotonic timestamp of the last successful push.
        self._last_push_ts: float = 0.0

    # ------------------------------------------------------------------ #
    # Sync helpers (issue #24)                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _vault_hash(vault_path: Path) -> str:
        """Return a short hash of the vault path for per-vault cache files."""
        return hashlib.sha256(str(vault_path).encode("utf-8")).hexdigest()[:16]

    def _pull_cache_path(self) -> Path:
        """Return the path to the per-vault last-pull timestamp cache file."""
        cache_dir = Path.home() / ".mnemos" / ".cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"sync-last-pull-{self._vault_hash(self._vault)}.ts"

    def _load_last_pull_ts(self) -> float:
        """Load the last-pull timestamp from disk.  Returns 0.0 if absent."""
        try:
            return float(self._pull_cache_path().read_text(encoding="utf-8").strip())
        except Exception:
            return 0.0

    def _save_last_pull_ts(self, ts: float) -> None:
        """Persist the last-pull timestamp to disk."""
        try:
            self._pull_cache_path().write_text(str(ts), encoding="utf-8")
        except Exception:
            pass  # non-fatal — the in-memory value is still correct

    def _has_remote(self) -> bool:
        """Return ``True`` when the configured remote exists in the vault repo.

        Uses :func:`core.git.remote_exists` (which runs ``git remote get-url``).
        Returns ``False`` on any error (git not found, not a repo, etc.) so
        callers can treat the absence of a remote as local-only mode.
        """
        try:
            import core.git as _git
            return _git.remote_exists(self._vault, self._sync.remote)
        except Exception:
            return False

    def _should_pull(self) -> bool:
        """Return ``True`` when a pull should be attempted now.

        A pull is skipped when:
        - ``pull_rate_limit_seconds`` has NOT yet elapsed since the last pull, OR
        - the configured remote does not exist in the vault repo (local-only mode), OR
        - the remote branch does not yet exist (empty / uninitialized remote repo).
        """
        if not self._sync.enabled or not self._sync.auto_pull_on_capture:
            return False
        if not self._has_remote():
            return False
        # Skip pull when the remote branch doesn't exist yet (e.g. empty GitHub
        # repo before the first push).  Treat the same as local-only mode.
        try:
            import core.git as _git
            if not _git.remote_has_branch(
                self._vault, self._sync.remote, self._sync.branch
            ):
                return False
        except Exception:
            return False
        elapsed = time.monotonic() - self._last_pull_ts
        # _last_pull_ts is 0.0 on the first-ever call, so elapsed will be huge
        # and a pull is always attempted.
        return elapsed >= self._sync.pull_rate_limit_seconds

    def _hook_before_write(self) -> None:
        """Hook 1 — attempt a pull-rebase before the disk write.

        Runs only when ``sync.enabled`` is ``True`` AND the rate-limit window
        has expired.  On pull failure (conflict), writes conflict artefacts and
        raises :exc:`SyncConflictError`.
        """
        if not self._sync.enabled:
            return
        if not self._should_pull():
            return
        import core.git as _git
        try:
            _git.pull_rebase(
                self._vault,
                remote=self._sync.remote,
                branch=self._sync.branch,
                autostash=True,
            )
            now = time.monotonic()
            self._last_pull_ts = now
            self._save_last_pull_ts(now)
        except _git.GitCommandError as exc:
            # pull_rebase failed — surface a conflict
            self._write_conflict_artefacts(str(exc))
            raise SyncConflictError(
                f"git pull --rebase failed: {exc}",
            ) from exc

    def _write_conflict_artefacts(self, error_detail: str) -> None:
        """Write ``_sync_conflict.md`` at the vault root.

        We cannot reliably determine which item ids are conflicting without
        parsing git's conflict output, so we write a general notice.
        """
        conflict_path = self._vault / "_sync_conflict.md"
        lines = [
            "# mnemos Sync Conflict",
            "",
            "_A ``git pull --rebase`` conflict was detected._",
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
            "1. Open this vault in your terminal.",
            "2. Resolve the conflict markers in the affected ``.md`` files.",
            "3. Run ``git add <resolved-files>``.",
            "4. Run ``mnemos sync continue`` (or ``git rebase --continue``).",
            "5. Delete this file after the conflict is resolved.",
            "",
        ]
        conflict_path.write_text("\n".join(lines), encoding="utf-8")

    def _hook_after_write(self, changed_paths: list[Path]) -> bool:
        """Hook 2 — stage and commit changed paths after a successful disk write.

        Returns ``True`` if a commit was actually created, ``False`` otherwise
        (nothing to commit / sync disabled).
        """
        if not self._sync.enabled:
            return False
        if not changed_paths:
            return False
        import core.git as _git
        _git.add(self._vault, [str(p) for p in changed_paths])
        # Build commit message
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        # Use generic layer/id for multi-file operations; single-file writes
        # fill in the actual values via the overloaded _hook_after_write_item.
        message = self._sync.commit_message_template.format(
            layer="vault",
            id=",".join(p.stem for p in changed_paths[:3]),
            timestamp=timestamp,
        )
        committed = _git.commit(self._vault, message)
        return committed

    def _hook_after_write_item(
        self,
        layer: str,
        item_id: str,
        changed_paths: list[Path],
    ) -> bool:
        """Hook 2 (item-level) — commit with a message that names the item.

        Returns ``True`` if a commit was created.
        """
        if not self._sync.enabled:
            return False
        if not changed_paths:
            return False
        import core.git as _git
        _git.add(self._vault, [str(p) for p in changed_paths])
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        message = self._sync.commit_message_template.format(
            layer=layer,
            id=item_id,
            timestamp=timestamp,
        )
        committed = _git.commit(self._vault, message)
        return committed

    def _hook_after_commit(self, committed: bool) -> None:
        """Hook 3 — push after a successful commit.

        Only runs when ``committed`` is ``True`` AND ``auto_push_after_commit``
        is set AND a remote is configured with the target branch already
        existing on the remote.  When no remote is configured or the remote
        branch does not yet exist (uninitialized repo), the push is skipped
        silently.
        """
        if not self._sync.enabled:
            return
        if not committed:
            return
        if not self._sync.auto_push_after_commit:
            return
        if not self._has_remote():
            # Local-only mode — no remote configured, skip push silently
            return
        import core.git as _git
        # Skip push when the remote branch hasn't been created yet (empty repo)
        if not _git.remote_has_branch(self._vault, self._sync.remote, self._sync.branch):
            return
        _git.push(self._vault, remote=self._sync.remote, branch=self._sync.branch)
        self._last_push_ts = time.monotonic()

    # ------------------------------------------------------------------ #
    # Public sync API                                                       #
    # ------------------------------------------------------------------ #

    def sync_pull(self) -> None:
        """Manual pull — works regardless of ``mode`` or rate limit.

        When no remote is configured, or the remote branch does not yet exist
        (uninitialized remote repo), this method returns silently without
        raising an error.

        Raises :exc:`SyncConflictError` when the pull detects a conflict.
        """
        if not self._has_remote():
            # Local-only mode — no remote to pull from
            return
        import core.git as _git
        if not _git.remote_has_branch(self._vault, self._sync.remote, self._sync.branch):
            # Remote exists but the branch hasn't been pushed yet — skip silently
            return
        try:
            _git.pull_rebase(
                self._vault,
                remote=self._sync.remote,
                branch=self._sync.branch,
                autostash=True,
            )
            now = time.monotonic()
            self._last_pull_ts = now
            self._save_last_pull_ts(now)
        except _git.GitCommandError as exc:
            self._write_conflict_artefacts(str(exc))
            raise SyncConflictError(f"git pull --rebase failed: {exc}") from exc

    def sync_push(self) -> None:
        """Manual push to the configured remote/branch.

        When no remote is configured (local-only mode), this method returns
        silently without raising an error.

        Raises :exc:`~core.git.GitCommandError` on push failure.
        """
        if not self._has_remote():
            # Local-only mode — no remote to push to
            return
        import core.git as _git
        _git.push(self._vault, remote=self._sync.remote, branch=self._sync.branch)
        self._last_push_ts = time.monotonic()

    def sync_commit(self, message: str | None = None) -> bool:
        """Stage all tracked changes and create a commit.

        Parameters
        ----------
        message:
            Optional commit message override.  When ``None``, a generic
            timestamp-based message is used.

        Returns ``True`` if a commit was created, ``False`` if the tree was clean.
        """
        import core.git as _git
        _git.add(self._vault, ["."])
        if message is None:
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            message = f"mnemos: manual sync commit ({timestamp})"
        return _git.commit(self._vault, message)

    def sync_status(self) -> dict[str, object]:
        """Return a dict describing the current sync state.

        Keys
        ----
        branch : str
        ahead : int
        behind : int
        dirty : bool
        remote : str | None
        upstream : str | None
        untracked : int
        last_pull_ts : float
            Epoch time of the last successful pull (0.0 if never pulled).
        last_push_ts : float
            Epoch time of the last successful push in this process (0.0 if
            never pushed; not persisted across restarts).
        sync_enabled : bool
        sync_remote : str
        sync_branch : str
        """
        import core.git as _git
        stat = _git.status(self._vault)
        stat["last_pull_ts"] = self._last_pull_ts
        stat["last_push_ts"] = self._last_push_ts
        stat["sync_enabled"] = self._sync.enabled
        stat["sync_remote"] = self._sync.remote
        stat["sync_branch"] = self._sync.branch
        return stat

    def sync_continue(self) -> None:
        """Continue after a manually-resolved rebase conflict.

        Steps:
        1. Verify no conflict markers remain in any tracked ``.md`` file.
        2. ``git rebase --continue``.
        3. Delete ``_sync_conflict.md`` and ``transient/conflict-*.md``.

        Raises
        ------
        SyncConflictError
            When conflict markers (``<<<<<<<``) are still found in vault files.
        """
        # 1. Check for unresolved conflict markers
        conflicted: list[str] = []
        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in layer_dir.glob("*.md"):
                try:
                    text = md_file.read_text(encoding="utf-8")
                except Exception:
                    continue
                if "<<<<<<<" in text:
                    conflicted.append(str(md_file))

        if conflicted:
            raise SyncConflictError(
                "Conflict markers still present in vault files. "
                "Resolve all conflicts before running 'mnemos sync continue'.\n"
                + "\n".join(conflicted)
            )

        # 2. git rebase --continue — via core.git to satisfy the DIP rule.
        import core.git as _git
        try:
            _git.rebase_continue(self._vault)
        except _git.GitCommandError:
            raise

        # 3. Clean up conflict artefacts
        conflict_index = self._vault / "_sync_conflict.md"
        if conflict_index.exists():
            conflict_index.unlink()

        transient_dir = self._vault / "transient"
        if transient_dir.exists():
            for f in transient_dir.glob("conflict-*.md"):
                f.unlink()

        # Update the last-pull timestamp so the rate limit resets
        now = time.monotonic()
        self._last_pull_ts = now
        self._save_last_pull_ts(now)

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _layer_dir(self, layer: str) -> Path:
        """Return (and create) the vault sub-directory for *layer*."""
        if layer not in OBSIDIAN_LAYERS:
            raise ValueError(f"Unknown layer: {layer!r}")
        d = self._vault / layer
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _find_path(self, item_id: str) -> Path:
        """Search all layer folders for an item with the given *item_id*.

        First tries the legacy ``<item_id>.md`` filename for backward
        compatibility with pre-slug vaults.  If not found, scans all
        ``.md`` files in all layers and checks the ``id`` front-matter
        field (used by slug-named files written after this change).

        Raises :exc:`FileNotFoundError` if the item is not found anywhere.
        """
        # Fast path: legacy UUID-named file (backward compat)
        for layer in OBSIDIAN_LAYERS:
            candidate = self._vault / layer / f"{item_id}.md"
            if candidate.exists():
                return candidate
        # Slow path: scan frontmatter for matching id (slug-named files)
        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in sorted(layer_dir.glob("*.md")):
                try:
                    post = frontmatter.load(str(md_file))
                    if post.metadata.get("id") == item_id:
                        return md_file
                except Exception:
                    continue
        raise FileNotFoundError(
            f"Memory item not found in Obsidian vault: {item_id!r}"
        )

    def _build_file_path(self, layer_dir: Path, item_id: str, content: str) -> Path:
        """Return the slug-based file path for *item_id*/*content*.

        If ``<slug>.md`` is absent, that path is returned immediately.
        If it exists and belongs to *item_id*, it is returned (idempotent
        rewrite).  If it belongs to a different item, numbered variants are
        tried: ``<slug>-2.md``, ``<slug>-3.md``, …
        """
        slug = _content_slug(content)
        candidate = layer_dir / f"{slug}.md"

        if not candidate.exists():
            return candidate

        # Check if the existing file belongs to this item_id
        try:
            existing = frontmatter.load(str(candidate))
            if existing.metadata.get("id") == item_id:
                return candidate  # idempotent rewrite
        except Exception:
            pass

        # Collision with a different item — try numbered suffixes
        counter = 2
        while True:
            candidate = layer_dir / f"{slug}-{counter}.md"
            if not candidate.exists():
                return candidate
            try:
                existing = frontmatter.load(str(candidate))
                if existing.metadata.get("id") == item_id:
                    return candidate
            except Exception:
                pass
            counter += 1

    def _resolve_path(self, item_id_or_path: str) -> Path:
        """Resolve *item_id_or_path* to a :class:`~pathlib.Path`.

        Accepts:
        - An absolute file path that exists.
        - A relative path under the vault.
        - A bare ``item_id`` (searches all layer folders).
        """
        p = Path(item_id_or_path)
        if p.is_absolute() and p.exists():
            return p
        if (self._vault / item_id_or_path).exists():
            return self._vault / item_id_or_path
        # Treat as item_id
        return self._find_path(item_id_or_path)

    def _parse_path(self, path: Path) -> dict[str, Any]:
        """Parse a vault ``.md`` file into a dict with ``content`` + ``_path``."""
        post = frontmatter.load(str(path))
        result = dict(post.metadata)
        result["content"] = post.content
        result["_path"] = str(path)
        return result

    def _write_path(self, path: Path, content: str, metadata: dict[str, Any]) -> None:
        """Write a frontmatter document to *path*."""
        post = frontmatter.Post(content, **metadata)
        with path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

    def _fts_index(self, item_id: str, content: str, metadata: dict[str, Any]) -> None:
        """Index *item_id* in FTS (no-op when FTS is not configured)."""
        if self._fts is not None:
            self._fts.index_item(
                item_id=item_id,
                content=content,
                metadata={k: v for k, v in metadata.items()
                          if k not in ("content", "_path")},
            )

    def _fts_delete(self, item_id: str) -> None:
        """Remove *item_id* from FTS (no-op when FTS is not configured)."""
        if self._fts is not None:
            self._fts.remove(item_id)

    # ------------------------------------------------------------------ #
    # StorageBackend Protocol                                               #
    # ------------------------------------------------------------------ #

    def write(
        self,
        layer: str,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Create or overwrite a vault file and return its path.

        ``run_id`` and ``session_id`` are accepted for Protocol compatibility
        but ignored — all items are stored directly in ``<vault>/<layer>/``.

        When ``sync.enabled`` is ``True``, this method:
        1. Attempts a rate-limited pull before writing.
        2. Commits the changed file after writing.
        3. Pushes the commit if ``auto_push_after_commit`` is set.
        """
        # Hook 1 — pull before write (may raise SyncConflictError)
        self._hook_before_write()

        layer_dir = self._layer_dir(layer)
        file_path = self._build_file_path(layer_dir, item_id, content)

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        meta = dict(metadata)
        # Ensure required front-matter fields are present
        meta.setdefault("id", item_id)
        meta.setdefault("layer", layer)
        meta.setdefault("tags", [])
        meta.setdefault("quality_score", 0.8)
        meta.setdefault("created_at", now)
        meta.setdefault("access_count", 0)
        meta["updated_at"] = now
        meta["content_hash"] = _content_hash(content)

        self._write_path(file_path, content, meta)
        self._mtime_cache[str(file_path)] = file_path.stat().st_mtime
        self._fts_index(item_id, content, meta)

        # Hook 2 — commit, Hook 3 — push
        committed = self._hook_after_write_item(layer, item_id, [file_path])
        self._hook_after_commit(committed)

        return file_path

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        """Return a memory item dict (``content`` + front-matter + ``_path``)."""
        path = self._resolve_path(item_id_or_path)
        return self._parse_path(path)

    def update(
        self,
        item_id_or_path: str,
        content: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Path:
        """Update content and/or metadata of an existing vault item."""
        # Hook 1 — pull before write
        self._hook_before_write()

        path = self._resolve_path(item_id_or_path)
        item = self._parse_path(path)

        new_content = content if content is not None else item["content"]
        new_meta = {k: v for k, v in item.items() if k not in ("content", "_path")}
        if metadata_updates:
            new_meta.update(metadata_updates)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        new_meta["updated_at"] = now
        if content is not None:
            new_meta["content_hash"] = _content_hash(new_content)

        self._write_path(path, new_content, new_meta)
        self._mtime_cache[str(path)] = path.stat().st_mtime
        item_id = new_meta.get("id", path.stem)
        self._fts_index(item_id, new_content, new_meta)

        # Hook 2 — commit, Hook 3 — push
        layer = str(new_meta.get("layer", path.parent.name))
        committed = self._hook_after_write_item(layer, item_id, [path])
        self._hook_after_commit(committed)

        return path

    def delete(self, item_id_or_path: str) -> None:
        """Remove a vault item file."""
        path = self._resolve_path(item_id_or_path)
        item_id = self._parse_path(path).get("id", path.stem)
        self._mtime_cache.pop(str(path), None)
        path.unlink()
        self._fts_delete(item_id)

    def list_layer(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Path]:
        """Yield all item paths in the given layer folder."""
        if layer not in OBSIDIAN_LAYERS:
            return
        layer_dir = self._vault / layer
        if layer_dir.exists():
            yield from sorted(layer_dir.glob("*.md"))

    def parse_file(self, path: Path) -> dict[str, Any]:
        """Parse a single vault file and return its dict representation.

        Part of the :class:`~core.store.StorageBackend` Protocol surface.
        """
        return self._parse_path(path)

    def iter_layer_items(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield parsed item dicts for every item in *layer*."""
        for path in self.list_layer(layer, run_id=run_id, session_id=session_id):
            try:
                yield self._parse_path(path)
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # Promotion (layer → layer file move)                                  #
    # ------------------------------------------------------------------ #

    def promote(self, item_id_or_path: str, target_layer: str) -> Path:
        """Move an item to *target_layer* and update its ``layer`` front-matter field.

        Implements Q3 decision: promotion = git-move (``os.rename``) + front-matter update.

        When sync is enabled the promotion is committed as a single git commit
        that includes both the deletion from the source layer and the creation
        in the target layer (matching the "git mv shape" requirement from #24).
        """
        # Hook 1 — pull before write
        self._hook_before_write()

        src_path = self._resolve_path(item_id_or_path)
        item = self._parse_path(src_path)
        item_id = item.get("id", src_path.stem)

        target_dir = self._layer_dir(target_layer)
        # Preserve the slug-based filename when moving between layers
        dst_path = target_dir / src_path.name

        # Update layer in metadata
        item["layer"] = target_layer
        item["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        content = item.pop("content")
        item.pop("_path", None)

        # Atomic move
        self._write_path(dst_path, content, item)
        src_path.unlink()

        self._mtime_cache.pop(str(src_path), None)
        self._mtime_cache[str(dst_path)] = dst_path.stat().st_mtime
        self._fts_index(item_id, content, item)

        # Hook 2 — commit both the src deletion and the dst creation together
        if self._sync.enabled:
            import core.git as _git
            # Stage both paths (deletion + creation) in one commit
            _git.add(self._vault, [str(src_path), str(dst_path)])
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            message = self._sync.commit_message_template.format(
                layer=target_layer,
                id=item_id,
                timestamp=timestamp,
            )
            committed = _git.commit(self._vault, message)
            # Hook 3 — push
            self._hook_after_commit(committed)

        return dst_path

    # ------------------------------------------------------------------ #
    # Bidirectional edit detection                                          #
    # ------------------------------------------------------------------ #

    def sync_edits(self) -> int:
        """Detect externally edited vault files and reconcile metadata.

        Scans every ``.md`` file in every layer folder.  For each file:

        - If the file's current mtime differs from the cached mtime (or the
          file is not yet cached), recompute ``content_hash``, increment
          ``access_count``, and update ``updated_at`` in front-matter.
        - Unchanged files are skipped.

        Returns the number of files that were updated.

        This implements the Q2 decision: bg-check calls ``sync_edits()``
        periodically so that user edits made directly in Obsidian are
        reflected in mnemos metadata.
        """
        changed = 0
        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in layer_dir.glob("*.md"):
                path_str = str(md_file)
                try:
                    current_mtime = md_file.stat().st_mtime
                except OSError:
                    continue

                cached_mtime = self._mtime_cache.get(path_str)
                if cached_mtime is not None and abs(current_mtime - cached_mtime) < 1e-6:
                    # File unchanged since last sync
                    continue

                # File is new or modified — reconcile
                try:
                    item = self._parse_path(md_file)
                except Exception:
                    continue

                content = item.get("content", "")
                new_hash = _content_hash(content)

                # Only count as "changed" if hash actually differs
                old_hash = item.get("content_hash", "")
                if new_hash == old_hash and cached_mtime is not None:
                    # mtime changed but content is identical (e.g. save without edit)
                    self._mtime_cache[path_str] = current_mtime
                    continue

                # Build updated metadata
                new_meta = {k: v for k, v in item.items() if k not in ("content", "_path")}
                new_meta["content_hash"] = new_hash
                new_meta["access_count"] = int(new_meta.get("access_count", 0)) + 1
                new_meta["updated_at"] = (
                    datetime.datetime.now(datetime.timezone.utc).isoformat()
                )

                self._write_path(md_file, content, new_meta)
                self._mtime_cache[path_str] = md_file.stat().st_mtime
                item_id = new_meta.get("id", md_file.stem)
                self._fts_index(item_id, content, new_meta)
                changed += 1

        return changed

    # ------------------------------------------------------------------ #
    # Health page (_health.md)                                             #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # UUID-to-slug migration                                               #
    # ------------------------------------------------------------------ #

    def rename_uuid_to_slug(
        self,
        dry_run: bool = False,
        commit: bool = False,
    ) -> dict[str, object]:
        """Rename all UUID-named vault files to slug-based names.

        Returns stats: {renamed: int, skipped: int, dry_run: bool, renames: list[dict]}
        Each rename dict: {layer, old_name, new_name, item_id, reason}
        reason is "renamed", "skipped" (already slug), or "collision_resolved".
        """
        UUID_RE = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE,
        )

        renames: list[dict[str, object]] = []
        renamed = 0
        skipped = 0
        changed_paths: list[Path] = []

        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in sorted(layer_dir.glob("*.md")):
                stem = md_file.stem
                # Check if the filename stem is a UUID
                if not UUID_RE.fullmatch(stem):
                    skipped += 1
                    renames.append({
                        "layer": layer,
                        "old_name": md_file.name,
                        "new_name": md_file.name,
                        "item_id": stem,
                        "reason": "skipped",
                    })
                    continue

                # Parse frontmatter to get id and content
                try:
                    post = frontmatter.load(str(md_file))
                except Exception:
                    skipped += 1
                    renames.append({
                        "layer": layer,
                        "old_name": md_file.name,
                        "new_name": md_file.name,
                        "item_id": stem,
                        "reason": "skipped",
                    })
                    continue

                item_id = post.metadata.get("id", stem)
                content = post.content

                # Build slug-based destination path (handles collision)
                new_path = self._build_file_path(layer_dir, item_id, content)
                new_name = new_path.name

                if new_path == md_file:
                    # Already at the right slug path (shouldn't happen for UUID files, but be safe)
                    skipped += 1
                    renames.append({
                        "layer": layer,
                        "old_name": md_file.name,
                        "new_name": new_name,
                        "item_id": item_id,
                        "reason": "skipped",
                    })
                    continue

                # Determine reason label
                slug = _content_slug(content)
                base_candidate = layer_dir / f"{slug}.md"
                reason = "renamed"
                if new_path != base_candidate:
                    reason = "collision_resolved"

                if not dry_run:
                    os.rename(str(md_file), str(new_path))
                    # Update mtime cache
                    self._mtime_cache.pop(str(md_file), None)
                    try:
                        self._mtime_cache[str(new_path)] = new_path.stat().st_mtime
                    except OSError:
                        pass
                    changed_paths.extend([md_file, new_path])

                renamed += 1
                renames.append({
                    "layer": layer,
                    "old_name": md_file.name,
                    "new_name": new_name,
                    "item_id": item_id,
                    "reason": reason,
                })

        # Optionally commit
        if commit and not dry_run and changed_paths:
            if self._sync.enabled:
                import core.git as _git
                _git.add(self._vault, [str(p) for p in changed_paths])
                _git.commit(self._vault, "mnemos: rename UUID files to slug names")
            else:
                # Try committing even if sync is not enabled (standalone commit mode)
                try:
                    import core.git as _git
                    _git.add(self._vault, [str(p) for p in changed_paths])
                    _git.commit(self._vault, "mnemos: rename UUID files to slug names")
                except Exception:
                    pass

        return {
            "renamed": renamed,
            "skipped": skipped,
            "dry_run": dry_run,
            "renames": renames,
        }

    def generate_health_page(self) -> Path:
        """Generate (or regenerate) ``_health.md`` at the vault root.

        The page contains four sections — **Orphans**, **Stale Items**,
        **Low Quality**, and a sync disclaimer — with ``[[wikilinks]]`` to
        every flagged item.

        The file is fully regenerated on each call (idempotent).

        Implements Q4 decision.
        """
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        low_quality: list[str] = []
        stale: list[str] = []
        orphans: list[str] = []

        stale_threshold = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        for layer in OBSIDIAN_LAYERS:
            layer_dir = self._vault / layer
            if not layer_dir.exists():
                continue
            for md_file in layer_dir.glob("*.md"):
                if md_file.name == "_health.md":
                    continue
                try:
                    item = self._parse_path(md_file)
                except Exception:
                    orphans.append(md_file.stem)
                    continue

                item_id = item.get("id", md_file.stem)
                quality = float(item.get("quality_score", 0.8))
                if quality < LOW_QUALITY_THRESHOLD:
                    low_quality.append(item_id)

                # Stale: updated_at older than 30 days
                updated_str = item.get("updated_at", "")
                if updated_str:
                    try:
                        updated_dt = datetime.datetime.fromisoformat(
                            updated_str.replace("Z", "+00:00")
                        )
                        if updated_dt < stale_threshold:
                            stale.append(item_id)
                    except ValueError:
                        pass

        def _wikilinks(ids: list[str]) -> str:
            if not ids:
                return "_None_"
            return "\n".join(f"- [[{item_id}]]" for item_id in sorted(ids))

        lines: list[str] = [
            "# mnemos Health",
            "",
            "_Generated: " + now + "_",
            "",
            "> **Note — Multi-host sync**: The Obsidian backend stores items locally "
            "in this vault. Multi-host sync (iCloud, git) is **out of scope** for "
            "this release and is the user's responsibility.",
            "",
            "## Orphans",
            "",
            "_Items that could not be parsed (corrupt front-matter):_",
            "",
            _wikilinks(orphans),
            "",
            "## Stale Items",
            "",
            "_Items not updated in the last 30 days:_",
            "",
            _wikilinks(stale),
            "",
            "## Low Quality",
            "",
            f"_Items with quality\\_score < {LOW_QUALITY_THRESHOLD}:_",
            "",
            _wikilinks(low_quality),
            "",
        ]

        health_path = self._vault / "_health.md"
        health_path.write_text("\n".join(lines), encoding="utf-8")
        return health_path
