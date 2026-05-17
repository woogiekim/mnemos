"""Tests for ObsidianBackend — Protocol conformance, CRUD, promotion,
bidirectional edit detection, FTS writes, and health page generation.

TDD: these tests were written before the implementation.
"""
from __future__ import annotations

import hashlib
import time
import unicodedata
import re
from pathlib import Path
from typing import Any

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fts(tmp_path: Path):
    """Create a temporary FTSIndex."""
    from core.fts import FTSIndex
    db_path = str(tmp_path / ".agent" / "state" / "fts.db")
    return FTSIndex(db_path=db_path)


def _make_backend(tmp_path: Path, vault_subdir: str = "vault"):
    """Create an ObsidianBackend rooted at tmp_path/vault."""
    from core.obsidian import ObsidianBackend
    vault_path = tmp_path / vault_subdir
    vault_path.mkdir(parents=True, exist_ok=True)
    fts = _make_fts(tmp_path)
    return ObsidianBackend(vault_path=str(vault_path), fts=fts)


def _canonical_hash(content: str) -> str:
    nfkc = unicodedata.normalize("NFKC", content)
    normalised = re.sub(r"\s+", " ", nfkc.strip()).lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestObsidianBackendProtocol:
    """ObsidianBackend must satisfy the StorageBackend Protocol at runtime."""

    def test_obsidian_backend_is_storage_backend(self, tmp_path):
        from core.store import StorageBackend
        backend = _make_backend(tmp_path)
        assert isinstance(backend, StorageBackend), (
            "ObsidianBackend must be recognised as StorageBackend via runtime_checkable"
        )

    def test_has_all_protocol_methods(self, tmp_path):
        from core.store import StorageBackend
        required = ["write", "read", "update", "delete", "list_layer",
                    "parse_file", "iter_layer_items"]
        backend = _make_backend(tmp_path)
        for method in required:
            assert hasattr(backend, method), f"ObsidianBackend must have method: {method}"


# ---------------------------------------------------------------------------
# write / read
# ---------------------------------------------------------------------------


class TestWriteRead:
    def test_write_creates_file_in_correct_folder(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        path = backend.write(
            layer="project",
            item_id="test-001",
            content="hello obsidian",
            metadata={"id": "test-001", "layer": "project", "tags": []},
        )
        assert (vault / "project" / "test-001.md").exists()
        assert str(path) == str(vault / "project" / "test-001.md")

    def test_write_creates_frontmatter(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="global",
            item_id="glo-001",
            content="global memory",
            metadata={
                "id": "glo-001",
                "layer": "global",
                "tags": ["important"],
                "quality_score": 0.9,
                "access_count": 0,
            },
        )
        vault = tmp_path / "vault"
        post = frontmatter.load(str(vault / "global" / "glo-001.md"))
        assert post.metadata["id"] == "glo-001"
        assert post.metadata["layer"] == "global"
        assert post.metadata["tags"] == ["important"]
        assert post.content == "global memory"

    def test_read_returns_content_and_path(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="session",
            item_id="sess-001",
            content="session content",
            metadata={"id": "sess-001", "layer": "session"},
        )
        result = backend.read("sess-001")
        assert result["content"] == "session content"
        assert result["id"] == "sess-001"
        assert "_path" in result

    def test_read_by_path(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        path = backend.write(
            layer="project",
            item_id="path-read-001",
            content="read by path",
            metadata={"id": "path-read-001", "layer": "project"},
        )
        result = backend.read(str(path))
        assert result["content"] == "read by path"

    def test_write_stores_content_hash_in_frontmatter(self, tmp_path):
        backend = _make_backend(tmp_path)
        content = "hash check content"
        backend.write(
            layer="global",
            item_id="hash-001",
            content=content,
            metadata={"id": "hash-001", "layer": "global"},
        )
        result = backend.read("hash-001")
        expected = _canonical_hash(content)
        assert result.get("content_hash") == expected

    def test_all_layers_map_to_folders(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        layers = ["session", "project", "global", "transient",
                  "ephemeral", "working", "entities", "claims", "topics"]
        for layer in layers:
            backend.write(
                layer=layer,
                item_id=f"{layer}-layer-test",
                content=f"content for {layer}",
                metadata={"id": f"{layer}-layer-test", "layer": layer},
            )
            assert (vault / layer / f"{layer}-layer-test.md").exists(), (
                f"Expected file in vault/{layer}/ for layer='{layer}'"
            )


# ---------------------------------------------------------------------------
# update / delete
# ---------------------------------------------------------------------------


class TestUpdateDelete:
    def test_update_content(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="upd-001",
            content="original",
            metadata={"id": "upd-001", "layer": "project"},
        )
        backend.update("upd-001", content="updated content")
        result = backend.read("upd-001")
        assert result["content"] == "updated content"

    def test_update_metadata(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="upd-meta-001",
            content="content",
            metadata={"id": "upd-meta-001", "layer": "project", "quality_score": 0.5},
        )
        backend.update("upd-meta-001", metadata_updates={"quality_score": 0.9})
        result = backend.read("upd-meta-001")
        assert result["quality_score"] == 0.9

    def test_update_content_refreshes_hash(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="global",
            item_id="hash-upd-001",
            content="original content",
            metadata={"id": "hash-upd-001", "layer": "global"},
        )
        new_content = "updated content"
        backend.update("hash-upd-001", content=new_content)
        result = backend.read("hash-upd-001")
        assert result["content_hash"] == _canonical_hash(new_content)

    def test_delete_removes_file(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="del-001",
            content="to be deleted",
            metadata={"id": "del-001", "layer": "project"},
        )
        vault = tmp_path / "vault"
        assert (vault / "project" / "del-001.md").exists()
        backend.delete("del-001")
        assert not (vault / "project" / "del-001.md").exists()

    def test_delete_nonexistent_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        with pytest.raises(FileNotFoundError):
            backend.delete("nonexistent-item")


# ---------------------------------------------------------------------------
# list_layer / iter_layer_items
# ---------------------------------------------------------------------------


class TestListAndIter:
    def test_list_layer_yields_paths(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="global",
            item_id="list-001",
            content="item 1",
            metadata={"id": "list-001", "layer": "global"},
        )
        backend.write(
            layer="global",
            item_id="list-002",
            content="item 2",
            metadata={"id": "list-002", "layer": "global"},
        )
        paths = list(backend.list_layer("global"))
        assert len(paths) == 2
        ids = {p.stem for p in paths}
        assert ids == {"list-001", "list-002"}

    def test_list_layer_empty_returns_nothing(self, tmp_path):
        backend = _make_backend(tmp_path)
        paths = list(backend.list_layer("global"))
        assert paths == []

    def test_iter_layer_items_yields_dicts(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="iter-001",
            content="iter content",
            metadata={"id": "iter-001", "layer": "project", "tags": ["t1"]},
        )
        items = list(backend.iter_layer_items("project"))
        assert len(items) == 1
        assert items[0]["id"] == "iter-001"
        assert items[0]["content"] == "iter content"
        assert "_path" in items[0]

    def test_iter_layer_items_multiple(self, tmp_path):
        backend = _make_backend(tmp_path)
        for i in range(3):
            backend.write(
                layer="session",
                item_id=f"iter-multi-{i:03d}",
                content=f"session item {i}",
                metadata={"id": f"iter-multi-{i:03d}", "layer": "session"},
            )
        items = list(backend.iter_layer_items("session"))
        assert len(items) == 3

    def test_iter_unknown_layer_yields_nothing(self, tmp_path):
        backend = _make_backend(tmp_path)
        items = list(backend.iter_layer_items("nonexistent_layer"))
        assert items == []


# ---------------------------------------------------------------------------
# parse_file
# ---------------------------------------------------------------------------


class TestParseFile:
    def test_parse_file_returns_dict(self, tmp_path):
        backend = _make_backend(tmp_path)
        path = backend.write(
            layer="global",
            item_id="parse-001",
            content="parse content",
            metadata={"id": "parse-001", "layer": "global"},
        )
        result = backend.parse_file(path)
        assert result["content"] == "parse content"
        assert result["id"] == "parse-001"
        assert result["_path"] == str(path)


# ---------------------------------------------------------------------------
# Promotion (file move)
# ---------------------------------------------------------------------------


class TestPromotion:
    def test_promote_moves_file_to_new_layer_folder(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        backend.write(
            layer="session",
            item_id="promo-001",
            content="promote me",
            metadata={"id": "promo-001", "layer": "session"},
        )
        assert (vault / "session" / "promo-001.md").exists()

        backend.promote("promo-001", target_layer="project")

        assert not (vault / "session" / "promo-001.md").exists(), (
            "File should be moved out of session/"
        )
        assert (vault / "project" / "promo-001.md").exists(), (
            "File should appear in project/"
        )

    def test_promote_updates_layer_frontmatter(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="session",
            item_id="promo-fm-001",
            content="promote frontmatter",
            metadata={"id": "promo-fm-001", "layer": "session"},
        )
        backend.promote("promo-fm-001", target_layer="global")
        result = backend.read("promo-fm-001")
        assert result["layer"] == "global"

    def test_promote_nonexistent_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        with pytest.raises(FileNotFoundError):
            backend.promote("nonexistent-item", target_layer="project")


# ---------------------------------------------------------------------------
# Bidirectional edit detection (sync_edits)
# ---------------------------------------------------------------------------


class TestSyncEdits:
    def test_sync_edits_detects_mtime_change(self, tmp_path):
        """When a vault file is edited externally, sync_edits reconciles the hash."""
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"

        backend.write(
            layer="project",
            item_id="edit-001",
            content="original content",
            metadata={"id": "edit-001", "layer": "project"},
        )

        file_path = vault / "project" / "edit-001.md"
        original_hash = backend.read("edit-001")["content_hash"]

        # Simulate external Obsidian edit — modify file content directly
        time.sleep(0.05)  # ensure mtime changes
        post = frontmatter.load(str(file_path))
        post.content = "externally edited content"
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))
        # Touch to ensure mtime is updated
        file_path.touch()

        # sync_edits should detect the change and update content_hash
        changed = backend.sync_edits()

        result = backend.read("edit-001")
        new_hash = _canonical_hash("externally edited content")
        assert result["content_hash"] == new_hash, (
            "sync_edits must update content_hash when file is externally edited"
        )
        assert changed >= 1, "sync_edits must report at least 1 changed file"

    def test_sync_edits_unchanged_file_not_touched(self, tmp_path):
        """Files not modified since last sync are not touched."""
        backend = _make_backend(tmp_path)
        backend.write(
            layer="global",
            item_id="unchanged-001",
            content="unchanged content",
            metadata={"id": "unchanged-001", "layer": "global"},
        )
        # First sync to record baseline mtimes
        backend.sync_edits()
        # Second sync — nothing should have changed
        changed = backend.sync_edits()
        assert changed == 0

    def test_sync_edits_increments_access_count_on_change(self, tmp_path):
        """When sync_edits detects a changed file it increments access_count."""
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        backend.write(
            layer="session",
            item_id="acc-001",
            content="access count test",
            metadata={"id": "acc-001", "layer": "session", "access_count": 5},
        )
        file_path = vault / "session" / "acc-001.md"

        time.sleep(0.05)
        post = frontmatter.load(str(file_path))
        post.content = "edited for access count"
        with file_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter.dumps(post))
        file_path.touch()

        backend.sync_edits()
        result = backend.read("acc-001")
        assert result["access_count"] == 6


# ---------------------------------------------------------------------------
# FTS writes
# ---------------------------------------------------------------------------


class TestFTSIntegration:
    def test_write_indexes_in_fts(self, tmp_path):
        """ObsidianBackend.write must index the item in FTS."""
        from core.fts import FTSIndex
        fts = _make_fts(tmp_path)
        from core.obsidian import ObsidianBackend
        vault = tmp_path / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        backend = ObsidianBackend(vault_path=str(vault), fts=fts)
        backend.write(
            layer="project",
            item_id="fts-001",
            content="searchable obsidian content",
            metadata={"id": "fts-001", "layer": "project"},
        )
        results = fts.search("searchable obsidian content")
        ids = [r["item_id"] for r in results]
        assert "fts-001" in ids

    def test_delete_removes_from_fts(self, tmp_path):
        """ObsidianBackend.delete must remove the item from FTS."""
        from core.fts import FTSIndex
        fts = _make_fts(tmp_path)
        from core.obsidian import ObsidianBackend
        vault = tmp_path / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        backend = ObsidianBackend(vault_path=str(vault), fts=fts)
        backend.write(
            layer="global",
            item_id="fts-del-001",
            content="to be deleted from fts",
            metadata={"id": "fts-del-001", "layer": "global"},
        )
        backend.delete("fts-del-001")
        results = fts.search("to be deleted from fts")
        ids = [r["item_id"] for r in results]
        assert "fts-del-001" not in ids


# ---------------------------------------------------------------------------
# generate_health_page
# ---------------------------------------------------------------------------


class TestHealthPage:
    def test_generate_health_page_creates_file(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        backend.generate_health_page()
        assert (vault / "_health.md").exists()

    def test_health_page_contains_sections(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        backend.generate_health_page()
        content = (vault / "_health.md").read_text()
        assert "## Orphans" in content
        assert "## Stale Items" in content
        assert "## Low Quality" in content

    def test_health_page_uses_wikilinks(self, tmp_path):
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        # Write a low-quality item to ensure it appears
        backend.write(
            layer="project",
            item_id="low-q-001",
            content="low quality",
            metadata={"id": "low-q-001", "layer": "project", "quality_score": 0.1},
        )
        backend.generate_health_page()
        content = (vault / "_health.md").read_text()
        # Low-quality item should be referenced with wikilink syntax
        assert "[[low-q-001]]" in content

    def test_health_page_regeneration_is_idempotent(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        first = (tmp_path / "vault" / "_health.md").read_text()
        backend.generate_health_page()
        second = (tmp_path / "vault" / "_health.md").read_text()
        # Content should be identical except possibly for timestamp
        # Strip the generated timestamp line for comparison
        def strip_ts(text):
            return "\n".join(
                line for line in text.splitlines()
                if not line.startswith("_Generated:")
            )
        assert strip_ts(first) == strip_ts(second)
