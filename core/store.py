"""Filesystem Store — read/write memory items as Markdown with YAML front-matter."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import frontmatter

from core.layers import LAYER_STATIC_PATHS


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

    def _find_by_id(self, item_id: str) -> Iterator[Path]:
        """Search all known layer directories for a file matching item_id."""
        search_dirs = [self._root / path for path in LAYER_STATIC_PATHS.values()]
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
