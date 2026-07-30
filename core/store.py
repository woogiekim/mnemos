"""Filesystem Store — read/write memory items as Markdown with YAML front-matter."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable
from urllib.parse import quote

import frontmatter

import core.git as git
from core.config import SyncConfig
from core.layers import LAYER_STATIC_PATHS, TRANSIENT_PATH
from core.sync import GitSyncEngine, SyncConflictError, SyncFailureInfo

# Re-exported so callers can ``from core.store import SyncConflictError`` and so
# the symbol is available alongside the default backend that now raises it.
__all__ = [
    "StorageBackend",
    "SyncableBackend",
    "MemoryStore",
    "SyncConflictError",
    "SyncFailureInfo",
]


# ---------------------------------------------------------------------------
# Storage abstraction — Dependency-Inversion Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol that every storage backend must satisfy.

    :class:`MemoryStore` is the default filesystem implementation.
    A future Obsidian adapter (PR-B) will provide an alternative implementation
    without touching any caller that depends on this interface.

    All methods that accept ``run_id`` / ``session_id`` accept ``None`` as
    "use the default namespace".  Implementations may ignore those parameters
    when the underlying storage does not support sub-namespacing.
    """

    def write(
        self,
        layer: str,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Persist a memory item and return its storage path."""
        ...

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        """Return a memory item dict (``content`` + front-matter keys + ``_path``)."""
        ...

    def update(
        self,
        item_id_or_path: str,
        content: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Path:
        """Update content and/or metadata of an existing item."""
        ...

    def delete(self, item_id_or_path: str) -> None:
        """Remove a memory item."""
        ...

    def list_layer(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Path]:
        """Yield all item *paths* in the given layer."""
        ...

    def parse_file(self, path: Path) -> dict[str, Any]:
        """Parse a single memory file and return its dict representation."""
        ...

    def iter_layer_items(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield parsed item dicts for every item in *layer*.

        Each yielded dict contains all YAML front-matter keys plus ``content``
        and ``_path`` (the absolute path as a string).  Dynamic layers
        (``ephemeral``, ``working``, ``session``) scan *all* run / session
        sub-directories when ``run_id`` / ``session_id`` is ``None``.
        """
        ...


# ---------------------------------------------------------------------------
# Optional sync capability marker (issue #69)
# ---------------------------------------------------------------------------


@runtime_checkable
class SyncableBackend(Protocol):
    """Marker Protocol for backends that support optional remote git-sync.

    Backends that implement the ``sync_*`` surface (``MemoryStore`` and
    ``ObsidianBackend``) satisfy this Protocol.  The CLI uses an
    ``isinstance(backend, SyncableBackend)`` check (plus the per-backend
    ``sync.enabled`` flag) to decide whether ``mnemos sync …`` may operate on
    the active backend — rather than hard-coding a class name.
    """

    def sync_pull(self) -> None: ...
    def sync_push(self) -> None: ...
    def sync_commit(self, message: str | None = None) -> bool: ...
    def sync_status(self) -> dict[str, Any]: ...
    def sync_continue(self) -> None: ...


def _normalise_content(content: str) -> str:
    """Return the canonical text form used for persisted content hashes."""
    nfkc = unicodedata.normalize("NFKC", content)
    return re.sub(r"\s+", " ", nfkc.strip()).lower()


def _content_hash(content: str) -> str:
    """Return SHA-256 hex digest of the canonical memory content."""
    return hashlib.sha256(_normalise_content(content).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Filesystem implementation
# ---------------------------------------------------------------------------


class MemoryStore:
    """Manages filesystem storage for memory items.

    By default the store performs no remote sync (``sync_config=None`` →
    behaviour identical to every release before issue #69).  When a
    :class:`~core.config.SyncConfig` with ``enabled=True`` is passed, the store
    drives a :class:`~core.sync.GitSyncEngine` around every :meth:`write` so the
    persistent ``wiki/`` memory layers are pulled / committed / pushed to a
    remote.

    Commit scope (issue #69 — Decision 2): the default backend stages **only**
    changed paths under ``wiki/``.  It never runs ``git add .`` at the repo
    root, so repository source code is never accidentally committed.  Ephemeral
    / working / session / transient writes live under ``.agent/`` (gitignored)
    and therefore produce a commit no-op.
    """

    def __init__(self, repo_root: str, sync_config: SyncConfig | None = None) -> None:
        self._root = Path(repo_root)
        self._sync: SyncConfig = sync_config or SyncConfig()
        # Build the shared engine.  The ``wiki/``-scoped stage filter is the
        # seam that enforces Decision 2.  The conflict artefact lives under
        # ``wiki/`` and the conflict scan covers the persistent wiki layers.
        self._sync_engine = GitSyncEngine(
            self._root,
            self._sync,
            stage_filter=self._wiki_stage_filter,
            conflict_filename=str(Path("wiki") / "_sync_conflict.md"),
            conflict_scan_dirs=self._wiki_scan_dirs,
        )

    # ------------------------------------------------------------------ #
    # Sync helpers (issue #69)                                             #
    # ------------------------------------------------------------------ #

    def _wiki_dir(self) -> Path:
        """Return the repo's ``wiki/`` directory (the committable sub-tree)."""
        return self._root / "wiki"

    def _wiki_stage_filter(self, paths: Any) -> list[Path]:
        """Keep only paths under ``wiki/`` — never stage repo-root source.

        Accepts either real changed file paths (from :meth:`write`) or the
        engine root sentinel (from :meth:`sync_commit`).  In the sentinel case
        the whole ``wiki/`` directory is returned so a manual commit stages all
        pending wiki changes without ever touching the repository root.
        """
        wiki = self._wiki_dir()
        out: list[Path] = []
        for p in paths:
            p = Path(p)
            if p == self._root:
                # sync_commit sentinel — stage the wiki sub-tree only.
                if wiki.exists():
                    out.append(wiki)
                continue
            try:
                p.resolve().relative_to(wiki.resolve())
            except (ValueError, OSError):
                continue  # outside wiki/ — excluded (Decision 2)
            out.append(p)
        return out

    def _wiki_scan_dirs(self) -> list[Path]:
        """Directories scanned for unresolved conflict markers (wiki layers)."""
        return [self._root / rel for rel in LAYER_STATIC_PATHS.values()]

    # ------------------------------------------------------------------ #
    # Public sync API (delegates to the shared engine)                     #
    # ------------------------------------------------------------------ #

    def sync_pull(self) -> None:
        """Manual pull (silent in local-only mode; raises on conflict)."""
        self._sync_engine.sync_pull()

    def sync_push(self) -> None:
        """Manual push (silent in local-only mode)."""
        self._sync_engine.sync_push()

    def sync_commit(self, message: str | None = None) -> bool:
        """Stage the changed ``wiki/`` paths and create a commit."""
        return self._sync_engine.sync_commit(message)

    def sync_status(self) -> dict[str, Any]:
        """Return a dict describing the current sync state."""
        return self._sync_engine.sync_status()

    def sync_continue(self) -> None:
        """Continue after a manually-resolved rebase conflict."""
        self._sync_engine.sync_continue()

    @property
    def last_sync_failure(self) -> SyncFailureInfo | None:
        """Return the most recent post-commit push failure for this store."""
        return self._sync_engine.last_push_failure

    @staticmethod
    def canonical_filename(item_id: str) -> str:
        """Return a cross-platform safe filename for a logical memory ID."""
        encoded = quote(item_id, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        return f"{encoded or 'untitled'}.md"

    def _layer_path(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        if layer == "ephemeral":
            if not run_id:
                run_id = "default"
            return self._root / ".agent" / "runs" / run_id / "scratch"
        elif layer == "working":
            if not run_id:
                run_id = "default"
            return self._root / ".agent" / "runs" / run_id / "working"
        elif layer == "session":
            if not session_id:
                session_id = "default"
            return self._root / ".agent" / "sessions" / session_id
        elif layer == "transient":
            # Flat directory — no run_id or session_id namespace.
            # Items are short-lived and shared across all processes; GC
            # collects them on a 1-hour staleness window.
            return self._root / TRANSIENT_PATH
        elif layer in LAYER_STATIC_PATHS:
            return self._root / LAYER_STATIC_PATHS[layer]
        else:
            raise ValueError(f"Unknown layer: '{layer}'")

    def write(
        self,
        layer: str,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Path:
        """Write a memory item as a Markdown file with YAML front-matter.

        When sync is enabled (issue #69) this method also runs the three sync
        hooks around the disk write: a rate-limited pull before writing, a
        ``wiki/``-scoped commit after writing, and a push after the commit.
        Writes outside ``wiki/`` (ephemeral / working / session / transient)
        are staged out by the wiki filter and therefore commit to nothing.
        """
        self._sync_engine.clear_last_push_failure()

        # Hook 1 — pull before write (may raise SyncConflictError).
        self._sync_engine.hook_before_write()

        layer_dir = self._layer_path(layer, run_id=run_id, session_id=session_id)
        layer_dir.mkdir(parents=True, exist_ok=True)

        file_path = layer_dir / self.canonical_filename(item_id)

        post_metadata = dict(metadata)
        post_metadata["id"] = item_id

        post = frontmatter.Post(content, **post_metadata)
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        # Hook 2 — commit (wiki-scoped), Hook 3 — push.
        committed = self._sync_engine.hook_after_write_item(layer, item_id, [file_path])
        self._sync_engine.hook_after_commit(committed)

        return file_path

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        """Read a memory item by ID (searches all layers) or by absolute/relative path."""
        path = Path(item_id_or_path)

        # If it looks like a path that exists, use it directly
        if path.exists() and path.is_file():
            return self._parse_file(path)

        # Try absolute path under repo root
        candidate = self._root / item_id_or_path
        if candidate.exists():
            return self._parse_file(candidate)

        # Search all layers by item_id
        for found_path in self._find_by_id(item_id_or_path):
            return self._parse_file(found_path)

        raise FileNotFoundError(f"Memory item not found: '{item_id_or_path}'")

    def _parse_file(self, path: Path) -> dict[str, Any]:
        post = frontmatter.load(str(path))
        result = dict(post.metadata)
        result["content"] = post.content
        result["_path"] = str(path)
        return result

    def parse_file(self, path: Path) -> dict[str, Any]:
        """Public alias for :meth:`_parse_file`.

        Part of the :class:`StorageBackend` Protocol surface.  Callers should
        prefer this over the private ``_parse_file`` name.
        """
        return self._parse_file(path)

    def _find_by_id(self, item_id: str) -> Iterator[Path]:
        """Search all known layer directories for a file matching item_id."""
        search_dirs = [self._root / path for path in LAYER_STATIC_PATHS.values()]
        # Include the transient flat directory
        transient_dir = self._root / TRANSIENT_PATH
        if transient_dir.exists():
            search_dirs.append(transient_dir)
        # Search only known .agent subdirs (avoids scanning state/, reports/, tools/, etc.)
        agent_runs = self._root / ".agent" / "runs"
        agent_sessions = self._root / ".agent" / "sessions"
        for agent_sub in (agent_runs, agent_sessions):
            if agent_sub.exists():
                search_dirs.extend(d for d in agent_sub.rglob("*") if d.is_dir())

        for d in search_dirs:
            d = Path(d)
            if d.is_dir():
                candidates = [d / self.canonical_filename(item_id), d / f"{item_id}.md"]
                for candidate in candidates:
                    if candidate.exists():
                        yield candidate
                        return

                for md_file in d.glob("*.md"):
                    try:
                        parsed = self._parse_file(md_file)
                    except Exception:
                        continue
                    if parsed.get("id") == item_id:
                        yield md_file
                        return

    def _delete_file(self, file_path: Path) -> None:
        """Delete one memory file and sync a tracked wiki removal when enabled."""
        resolved_root = self._root.resolve()
        resolved_path = file_path.resolve()
        relative_path = (
            resolved_path.relative_to(resolved_root)
            if resolved_path.is_relative_to(resolved_root)
            else None
        )
        should_sync = (
            relative_path is not None
            and self._sync.enabled
            and git.is_tracked(self._root, relative_path)
        )
        if should_sync:
            self._sync_engine.hook_before_write()

        try:
            item = self._parse_file(file_path)
        except Exception:
            item = {}

        layer = str(item.get("layer") or file_path.parent.name)
        item_id = str(item.get("id") or file_path.stem)
        file_path.unlink()

        if should_sync:
            committed = self._sync_engine.hook_after_write_item(
                layer,
                item_id,
                [file_path],
            )
            self._sync_engine.hook_after_commit(committed)

    def delete(self, item_id_or_path: str) -> None:
        """Remove a memory item file."""
        path = Path(item_id_or_path)
        if path.exists() and path.is_file():
            self._delete_file(path)
            return

        candidate = self._root / item_id_or_path
        if candidate.exists():
            self._delete_file(candidate)
            return

        for found_path in self._find_by_id(item_id_or_path):
            self._delete_file(found_path)
            return

        raise FileNotFoundError(f"Memory item not found for deletion: '{item_id_or_path}'")

    def list_layer(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Path]:
        """Yield all item paths in the given layer."""
        if layer in ("ephemeral", "working") and run_id is None:
            agent_runs = self._root / ".agent" / "runs"
            if not agent_runs.exists():
                return
            subdir = "scratch" if layer == "ephemeral" else "working"
            for run_dir in agent_runs.iterdir():
                if not run_dir.is_dir():
                    continue
                layer_dir = run_dir / subdir
                if layer_dir.exists():
                    yield from layer_dir.glob("*.md")
            return

        if layer == "session" and session_id is None:
            agent_sessions = self._root / ".agent" / "sessions"
            if agent_sessions.exists():
                yield from agent_sessions.rglob("*.md")
            return

        try:
            layer_dir = self._layer_path(layer, run_id=run_id, session_id=session_id)
        except ValueError:
            return
        if layer_dir.exists():
            yield from layer_dir.glob("*.md")

    def iter_layer_items(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield parsed item dicts for every item in *layer*.

        Dynamic layers (``ephemeral``, ``working``, ``session``) scan *all*
        run / session sub-directories when ``run_id`` / ``session_id`` is
        ``None``, mirroring the behaviour previously hard-coded in
        ``gateway.consolidate``, ``gateway.list_all``, and ``gc._iter_layer_paths``.

        Each yielded dict contains all YAML front-matter keys plus ``content``
        and ``_path`` (absolute path as a string).
        """
        if layer in ("ephemeral", "working"):
            agent_runs = self._root / ".agent" / "runs"
            if not agent_runs.exists():
                return
            sub = "scratch" if layer == "ephemeral" else "working"
            for rd in agent_runs.iterdir():
                if not rd.is_dir():
                    continue
                layer_dir = rd / sub
                if layer_dir.exists():
                    for md_file in layer_dir.glob("*.md"):
                        try:
                            yield self._parse_file(md_file)
                        except Exception:
                            continue
        elif layer == "session":
            agent_sessions = self._root / ".agent" / "sessions"
            if not agent_sessions.exists():
                return
            for md_file in agent_sessions.rglob("*.md"):
                try:
                    yield self._parse_file(md_file)
                except Exception:
                    continue
        elif layer == "transient":
            transient_dir = self._root / TRANSIENT_PATH
            if transient_dir.exists():
                for md_file in transient_dir.glob("*.md"):
                    try:
                        yield self._parse_file(md_file)
                    except Exception:
                        continue
        elif layer in LAYER_STATIC_PATHS:
            layer_dir = self._root / LAYER_STATIC_PATHS[layer]
            if layer_dir.exists():
                for md_file in layer_dir.glob("*.md"):
                    try:
                        yield self._parse_file(md_file)
                    except Exception:
                        continue
        # Unknown layers yield nothing (consistent with list_layer).

    def update(
        self,
        item_id_or_path: str,
        content: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Path:
        """Update content and/or metadata of an existing item."""
        item = self.read(item_id_or_path)
        file_path = Path(item["_path"])

        new_content = content if content is not None else item["content"]
        new_metadata = {k: v for k, v in item.items() if k not in ("content", "_path")}
        if metadata_updates:
            new_metadata.update(metadata_updates)
        if content is not None:
            new_metadata["content_hash"] = _content_hash(new_content)

        post = frontmatter.Post(new_content, **new_metadata)
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        return file_path

    def migrate_unsafe_filenames(self, dry_run: bool = False) -> list[dict[str, str]]:
        """Rename legacy unsafe filenames to canonical safe names.

        The logical memory ID is preserved in frontmatter. Existing safe files
        are skipped, and destination collisions receive a numeric suffix. When
        the store root is a git worktree, tracked files are renamed with
        ``git mv`` and untracked files fall back to a filesystem rename.
        """
        changes: list[dict[str, str]] = []
        dirs = [self._root / path for path in LAYER_STATIC_PATHS.values()]
        dirs.append(self._root / TRANSIENT_PATH)
        agent_root = self._root / ".agent"
        if agent_root.exists():
            dirs.extend(d for d in agent_root.rglob("*") if d.is_dir())

        for directory in dirs:
            if not directory.is_dir():
                continue
            for md_file in directory.glob("*.md"):
                try:
                    item = self._parse_file(md_file)
                except Exception:
                    continue
                item_id = item.get("id") or md_file.stem
                desired = directory / self.canonical_filename(str(item_id))
                if md_file.name == desired.name:
                    continue
                target = desired
                counter = 2
                while target.exists() and target != md_file:
                    target = directory / f"{desired.stem}-{counter}.md"
                    counter += 1
                changes.append({
                    "from": str(md_file),
                    "to": str(target),
                    "id": str(item_id),
                })
                if not dry_run:
                    self._rename_memory_file(md_file, target)

        return changes

    def _rename_memory_file(self, source: Path, target: Path) -> None:
        """Rename a memory file, preferring ``git mv`` inside git worktrees."""
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            old = source.relative_to(self._root)
            new = target.relative_to(self._root)
            git.mv(self._root, old, new)
        except Exception:
            source.rename(target)
