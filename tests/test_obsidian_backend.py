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
        # File should exist in the correct layer folder with a slug-based name
        assert path.exists()
        assert path.parent == vault / "project"
        # The id is preserved in frontmatter even though filename is slug-based
        assert backend.read("test-001")["id"] == "test-001"

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
        result = backend.read("glo-001")
        assert result["id"] == "glo-001"
        assert result["layer"] == "global"
        assert result["tags"] == ["important"]
        assert result["content"] == "global memory"

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

    def test_read_uses_item_path_index_without_frontmatter_scan(self, tmp_path, monkeypatch):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="indexed-read-001",
            content="indexed read content",
            metadata={"id": "indexed-read-001", "layer": "project"},
        )

        def fail_scan(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("read should use id path index instead of vault-wide scan")

        monkeypatch.setattr(backend, "_find_path", fail_scan)

        result = backend.read("indexed-read-001")
        assert result["id"] == "indexed-read-001"
        assert result["content"] == "indexed read content"

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
            item_id = f"{layer}-layer-test"
            path = backend.write(
                layer=layer,
                item_id=item_id,
                content=f"content for {layer}",
                metadata={"id": item_id, "layer": layer},
            )
            assert path.exists(), f"Expected file in vault/{layer}/ for layer='{layer}'"
            assert path.parent == vault / layer, (
                f"File should be inside vault/{layer}/"
            )
            assert backend.read(item_id)["id"] == item_id


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
        path = backend.write(
            layer="project",
            item_id="del-001",
            content="to be deleted",
            metadata={"id": "del-001", "layer": "project"},
        )
        assert path.exists()
        backend.delete("del-001")
        assert not path.exists()

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
        # id is in frontmatter, not necessarily in the filename
        ids = {backend.read(str(p))["id"] for p in paths}
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

    def test_iter_layer_items_skips_unparseable_file(self, tmp_path):
        """A file with malformed front-matter is skipped, not raised.

        ``iter_layer_items`` must keep yielding the valid items in a layer
        even when a sibling ``.md`` file has unparseable YAML front-matter
        (``except Exception: continue``). This deterministically covers that
        skip branch, which was previously only hit incidentally by a live
        ``mnemos`` run against the developer's real store.
        """
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="valid-001",
            content="valid content",
            metadata={"id": "valid-001", "layer": "project"},
        )
        # Drop a sibling file with malformed YAML front-matter into the same
        # layer folder so frontmatter.load() raises a ParserError on it.
        broken = tmp_path / "vault" / "project" / "broken.md"
        broken.write_text(
            "---\nkey: [unclosed\n:::bad yaml\n---\nbody\n", encoding="utf-8"
        )

        items = list(backend.iter_layer_items("project"))

        assert len(items) == 1
        assert items[0]["id"] == "valid-001"


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
        src_path = backend.write(
            layer="session",
            item_id="promo-001",
            content="promote me",
            metadata={"id": "promo-001", "layer": "session"},
        )
        assert src_path.exists()

        dst_path = backend.promote("promo-001", target_layer="project")

        assert not src_path.exists(), "File should be moved out of session/"
        assert dst_path.exists(), "File should appear in project/"
        assert dst_path.parent == vault / "project"
        # Slug-based filename is preserved across layers
        assert dst_path.name == src_path.name

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

        file_path = backend.write(
            layer="project",
            item_id="edit-001",
            content="original content",
            metadata={"id": "edit-001", "layer": "project"},
        )

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
        file_path = backend.write(
            layer="session",
            item_id="acc-001",
            content="access count test",
            metadata={"id": "acc-001", "layer": "session", "access_count": 5},
        )

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


# ---------------------------------------------------------------------------
# Local-only mode (no remote configured)
# ---------------------------------------------------------------------------


def _make_local_vault(tmp_path: Path) -> "ObsidianBackend":
    """Create an ObsidianBackend with sync enabled but no remote configured.

    The vault is a git repo (so commits work) but has no remote.
    sync.enabled=True to exercise the auto-sync code path.
    """
    import subprocess
    from core.config import SyncConfig
    from core.obsidian import ObsidianBackend

    vault_path = tmp_path / "local_vault"
    vault_path.mkdir(parents=True, exist_ok=True)

    # Initialise a git repo without adding a remote
    subprocess.run(["git", "init", str(vault_path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(vault_path), "config", "user.email", "test@mnemos.local"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(vault_path), "config", "user.name", "mnemos test"],
        capture_output=True, check=True,
    )

    cfg = SyncConfig(
        enabled=True,
        remote="origin",
        branch="main",
        auto_pull_on_capture=True,
        auto_push_after_commit=True,
        pull_rate_limit_seconds=0,
    )
    fts_path = str(tmp_path / ".agent" / "state" / "fts.db")
    from core.fts import FTSIndex
    fts = FTSIndex(db_path=fts_path)
    return ObsidianBackend(vault_path=str(vault_path), fts=fts, sync_config=cfg)


class TestLocalOnlyMode:
    """When sync is enabled but no remote is configured, push/pull skip silently."""

    def test_has_remote_returns_false_when_no_remote(self, tmp_path):
        """_has_remote() must return False when no remote is configured."""
        backend = _make_local_vault(tmp_path)
        assert backend._has_remote() is False

    def test_should_pull_returns_false_when_no_remote(self, tmp_path):
        """_should_pull() must return False when remote is absent (local-only)."""
        backend = _make_local_vault(tmp_path)
        assert backend._should_pull() is False

    def test_write_succeeds_without_remote(self, tmp_path):
        """write() must succeed silently in local-only mode (no remote)."""
        backend = _make_local_vault(tmp_path)
        vault_path = tmp_path / "local_vault"
        path = backend.write(
            layer="session",
            item_id="local-001",
            content="local only content",
            metadata={"id": "local-001", "layer": "session"},
        )
        assert path.exists(), "write() must create the vault file"
        assert path.parent == vault_path / "session", "File should be in session/ layer"
        assert backend.read("local-001")["id"] == "local-001"

    def test_write_creates_local_git_commit(self, tmp_path):
        """In local-only mode, write() still creates a local git commit."""
        import subprocess
        backend = _make_local_vault(tmp_path)
        vault_path = tmp_path / "local_vault"
        backend.write(
            layer="project",
            item_id="local-commit-001",
            content="commit without remote",
            metadata={"id": "local-commit-001", "layer": "project"},
        )
        result = subprocess.run(
            ["git", "-C", str(vault_path), "log", "--oneline"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert len(result.stdout.strip().splitlines()) >= 1, (
            "A local git commit should have been created"
        )

    def test_update_succeeds_without_remote(self, tmp_path):
        """update() must succeed silently in local-only mode."""
        backend = _make_local_vault(tmp_path)
        backend.write(
            layer="global",
            item_id="local-upd-001",
            content="original",
            metadata={"id": "local-upd-001", "layer": "global"},
        )
        path = backend.update("local-upd-001", content="updated content")
        assert path.exists()

    def test_sync_push_skips_silently_when_no_remote(self, tmp_path):
        """Manual sync_push() must return silently when no remote is configured."""
        backend = _make_local_vault(tmp_path)
        # Must not raise any exception
        backend.sync_push()

    def test_sync_pull_skips_silently_when_no_remote(self, tmp_path):
        """Manual sync_pull() must return silently when no remote is configured."""
        backend = _make_local_vault(tmp_path)
        # Must not raise any exception
        backend.sync_pull()

    def test_hook_after_commit_skips_push_when_no_remote(self, tmp_path):
        """_hook_after_commit() must skip push silently when no remote."""
        backend = _make_local_vault(tmp_path)
        # Should not raise even with committed=True
        backend._hook_after_commit(committed=True)


# ---------------------------------------------------------------------------
# Slug-based filenames
# ---------------------------------------------------------------------------


class TestSlugFilenames:
    """Files are named from content words, not UUID — id stays in frontmatter."""

    def test_filename_derived_from_content(self, tmp_path):
        """write() creates a slug-based filename from the content words."""
        backend = _make_backend(tmp_path)
        vault = tmp_path / "vault"
        path = backend.write(
            layer="project",
            item_id="slug-test-001",
            content="Architecture decision use SQLite for FTS",
            metadata={"id": "slug-test-001", "layer": "project"},
        )
        assert path.name == "architecture-decision-use-sqlite-for-fts.md"
        assert path.parent == vault / "project"

    def test_uuid_in_frontmatter_not_filename(self, tmp_path):
        """The UUID id is stored in frontmatter, not the filename stem."""
        import uuid
        backend = _make_backend(tmp_path)
        item_id = str(uuid.uuid4())
        path = backend.write(
            layer="global",
            item_id=item_id,
            content="User prefers explicit error handling",
            metadata={"id": item_id, "layer": "global"},
        )
        assert path.stem != item_id  # filename is slug, not UUID
        result = backend.read(item_id)
        assert result["id"] == item_id  # UUID preserved in frontmatter

    def test_read_by_id_works_with_slug_filename(self, tmp_path):
        """read(item_id) resolves correctly even with slug-based filename."""
        backend = _make_backend(tmp_path)
        backend.write(
            layer="session",
            item_id="find-by-id-001",
            content="some memorable content here",
            metadata={"id": "find-by-id-001", "layer": "session"},
        )
        result = backend.read("find-by-id-001")
        assert result["content"] == "some memorable content here"
        assert result["id"] == "find-by-id-001"

    def test_slug_collision_appends_counter(self, tmp_path):
        """When two items have the same content prefix, filenames get -2, -3 suffixes."""
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="col-001",
            content="duplicate slug content here",
            metadata={"id": "col-001", "layer": "project"},
        )
        path2 = backend.write(
            layer="project",
            item_id="col-002",
            content="duplicate slug content here",
            metadata={"id": "col-002", "layer": "project"},
        )
        assert path2.stem.endswith("-2")

    def test_idempotent_rewrite_same_path(self, tmp_path):
        """Rewriting the same item_id produces the same slug path."""
        backend = _make_backend(tmp_path)
        path1 = backend.write(
            layer="project",
            item_id="idem-001",
            content="idempotent content check",
            metadata={"id": "idem-001", "layer": "project"},
        )
        path2 = backend.write(
            layer="project",
            item_id="idem-001",
            content="idempotent content check",
            metadata={"id": "idem-001", "layer": "project"},
        )
        assert path1 == path2

    def test_korean_content_slug(self, tmp_path):
        """Korean content words appear in the slug filename."""
        backend = _make_backend(tmp_path)
        path = backend.write(
            layer="global",
            item_id="korean-001",
            content="아키텍처 결정 SQLite 사용",
            metadata={"id": "korean-001", "layer": "global"},
        )
        assert "아키텍처" in path.stem
        result = backend.read("korean-001")
        assert result["id"] == "korean-001"

    def test_promote_preserves_slug_filename(self, tmp_path):
        """promote() moves the file but keeps the same slug filename."""
        backend = _make_backend(tmp_path)
        src_path = backend.write(
            layer="session",
            item_id="slug-promo-001",
            content="memorable promoted item",
            metadata={"id": "slug-promo-001", "layer": "session"},
        )
        dst_path = backend.promote("slug-promo-001", target_layer="project")
        assert dst_path.name == src_path.name
        assert dst_path.parent.name == "project"
