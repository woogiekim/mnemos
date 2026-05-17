"""Tests for the StorageBackend Protocol and MemoryStore public surface.

PR-A: lock in the new abstraction surface extracted from MemoryStore.
"""
from __future__ import annotations

import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path):
    from core.store import MemoryStore
    return MemoryStore(repo_root=str(tmp_path))


# ---------------------------------------------------------------------------
# StorageBackend Protocol conformance
# ---------------------------------------------------------------------------


class TestStorageBackendProtocol:
    """MemoryStore must satisfy the StorageBackend Protocol at runtime."""

    def test_memory_store_is_storage_backend(self, tmp_path):
        from core.store import MemoryStore, StorageBackend
        store = MemoryStore(repo_root=str(tmp_path))
        assert isinstance(store, StorageBackend), (
            "MemoryStore must be recognised as a StorageBackend via runtime_checkable"
        )

    def test_protocol_has_iter_layer_items(self):
        from core.store import StorageBackend
        assert hasattr(StorageBackend, "iter_layer_items"), (
            "StorageBackend Protocol must declare iter_layer_items"
        )

    def test_protocol_has_parse_file(self):
        from core.store import StorageBackend
        assert hasattr(StorageBackend, "parse_file"), (
            "StorageBackend Protocol must declare parse_file"
        )


# ---------------------------------------------------------------------------
# parse_file — public alias
# ---------------------------------------------------------------------------


class TestParseFile:
    def test_parse_file_returns_content_and_path(self, tmp_path):
        """parse_file (public) returns the same dict as _parse_file (private)."""
        store = _make_store(tmp_path)
        store.write(
            layer="project",
            item_id="parse-test",
            content="hello world",
            metadata={"id": "parse-test", "layer": "project"},
        )
        wiki_dir = tmp_path / "wiki" / "projects"
        md_path = wiki_dir / "parse-test.md"
        assert md_path.exists()

        result = store.parse_file(md_path)
        assert result["content"] == "hello world"
        assert result["_path"] == str(md_path)
        assert result["id"] == "parse-test"

    def test_parse_file_matches_private_method(self, tmp_path):
        """parse_file and _parse_file must return identical dicts."""
        store = _make_store(tmp_path)
        store.write(
            layer="global",
            item_id="alias-test",
            content="alias content",
            metadata={"id": "alias-test", "layer": "global"},
        )
        md_path = tmp_path / "wiki" / "global" / "alias-test.md"
        public = store.parse_file(md_path)
        private = store._parse_file(md_path)
        assert public == private


# ---------------------------------------------------------------------------
# iter_layer_items — static layers
# ---------------------------------------------------------------------------


class TestIterLayerItemsStatic:
    def test_yields_item_for_static_layer(self, tmp_path):
        store = _make_store(tmp_path)
        store.write(
            layer="global",
            item_id="static-item",
            content="static content",
            metadata={"id": "static-item", "layer": "global"},
        )
        items = list(store.iter_layer_items("global"))
        assert len(items) == 1
        assert items[0]["content"] == "static content"
        assert items[0]["id"] == "static-item"
        assert "_path" in items[0]

    def test_yields_multiple_items(self, tmp_path):
        store = _make_store(tmp_path)
        for i in range(3):
            store.write(
                layer="project",
                item_id=f"item-{i}",
                content=f"content {i}",
                metadata={"id": f"item-{i}", "layer": "project"},
            )
        items = list(store.iter_layer_items("project"))
        assert len(items) == 3

    def test_empty_layer_yields_nothing(self, tmp_path):
        store = _make_store(tmp_path)
        items = list(store.iter_layer_items("global"))
        assert items == []

    def test_unknown_layer_yields_nothing(self, tmp_path):
        store = _make_store(tmp_path)
        items = list(store.iter_layer_items("nonexistent-layer"))
        assert items == []


# ---------------------------------------------------------------------------
# iter_layer_items — dynamic layers
# ---------------------------------------------------------------------------


class TestIterLayerItemsDynamic:
    def test_ephemeral_scans_all_run_dirs(self, tmp_path):
        """iter_layer_items('ephemeral') must scan all run sub-directories."""
        store = _make_store(tmp_path)
        # Manually create two run dirs with scratch items.
        for run_id in ("run-a", "run-b"):
            scratch = tmp_path / ".agent" / "runs" / run_id / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)
            store.write(
                layer="ephemeral",
                item_id=f"eph-{run_id}",
                content=f"eph content {run_id}",
                metadata={"id": f"eph-{run_id}", "layer": "ephemeral"},
                run_id=run_id,
            )
        items = list(store.iter_layer_items("ephemeral"))
        ids = {item.get("id") for item in items}
        assert "eph-run-a" in ids
        assert "eph-run-b" in ids

    def test_working_scans_all_run_dirs(self, tmp_path):
        store = _make_store(tmp_path)
        for run_id in ("run-x", "run-y"):
            store.write(
                layer="working",
                item_id=f"wrk-{run_id}",
                content=f"working content {run_id}",
                metadata={"id": f"wrk-{run_id}", "layer": "working"},
                run_id=run_id,
            )
        items = list(store.iter_layer_items("working"))
        ids = {item.get("id") for item in items}
        assert "wrk-run-x" in ids
        assert "wrk-run-y" in ids

    def test_session_scans_all_session_dirs(self, tmp_path):
        store = _make_store(tmp_path)
        for session_id in ("sess-1", "sess-2"):
            store.write(
                layer="session",
                item_id=f"ses-{session_id}",
                content=f"session content {session_id}",
                metadata={"id": f"ses-{session_id}", "layer": "session"},
                session_id=session_id,
            )
        items = list(store.iter_layer_items("session"))
        ids = {item.get("id") for item in items}
        assert "ses-sess-1" in ids
        assert "ses-sess-2" in ids

    def test_ephemeral_empty_when_no_runs_dir(self, tmp_path):
        store = _make_store(tmp_path)
        items = list(store.iter_layer_items("ephemeral"))
        assert items == []

    def test_transient_scans_flat_dir(self, tmp_path):
        store = _make_store(tmp_path)
        store.write(
            layer="transient",
            item_id="trans-item",
            content="transient content",
            metadata={"id": "trans-item", "layer": "transient"},
        )
        items = list(store.iter_layer_items("transient"))
        assert len(items) == 1
        assert items[0]["id"] == "trans-item"
