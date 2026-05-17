"""Filesystem Store — read/write memory items as Markdown with YAML front-matter."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

import frontmatter

from core.layers import LAYER_STATIC_PATHS, TRANSIENT_PATH


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
# Filesystem implementation
# ---------------------------------------------------------------------------


class MemoryStore:
    """Manages filesystem storage for memory items."""

    def __init__(self, repo_root: str) -> None:
        self._root = Path(repo_root)

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
        """Write a memory item as a Markdown file with YAML front-matter."""
        layer_dir = self._layer_path(layer, run_id=run_id, session_id=session_id)
        layer_dir.mkdir(parents=True, exist_ok=True)

        file_path = layer_dir / f"{item_id}.md"

        post = frontmatter.Post(content, **metadata)
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

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
                candidate = d / f"{item_id}.md"
                if candidate.exists():
                    yield candidate

    def delete(self, item_id_or_path: str) -> None:
        """Remove a memory item file."""
        path = Path(item_id_or_path)
        if path.exists() and path.is_file():
            path.unlink()
            return

        candidate = self._root / item_id_or_path
        if candidate.exists():
            candidate.unlink()
            return

        for found_path in self._find_by_id(item_id_or_path):
            found_path.unlink()
            return

        raise FileNotFoundError(f"Memory item not found for deletion: '{item_id_or_path}'")

    def list_layer(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[Path]:
        """Yield all item paths in the given layer."""
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

        post = frontmatter.Post(new_content, **new_metadata)
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))

        return file_path
