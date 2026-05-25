"""Focused store and gateway edge-path coverage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest


def _write_memory(path: Path, content: str = "body", **metadata: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(content, **metadata)), encoding="utf-8")


def test_memory_store_dynamic_layers_find_delete_iter_and_migrate_edges(tmp_path: Path) -> None:
    from core.store import MemoryStore

    store = MemoryStore(str(tmp_path))
    fresh_store = MemoryStore(str(tmp_path / "fresh"))
    assert list(fresh_store.iter_layer_items("session")) == []
    assert store.canonical_filename("") == "untitled.md"
    assert store._layer_path("ephemeral").as_posix().endswith("/.agent/runs/default/scratch")
    assert store._layer_path("working").as_posix().endswith("/.agent/runs/default/working")
    assert store._layer_path("session").as_posix().endswith("/.agent/sessions/default")
    with pytest.raises(ValueError):
        store._layer_path("missing")

    rel = Path("wiki/projects/relative.md")
    _write_memory(tmp_path / rel, "relative", id="relative", layer="project")
    assert store.read(str(rel))["id"] == "relative"

    unsafe = tmp_path / "wiki" / "projects" / "unsafe:name.md"
    _write_memory(unsafe, "unsafe", id="unsafe:name", layer="project")
    metadata_only = tmp_path / "wiki" / "projects" / "metadata-only.md"
    _write_memory(metadata_only, "metadata", id="metadata-id", layer="project")
    bad = tmp_path / "wiki" / "projects" / "bad.md"
    bad.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    found = list(store._find_by_id("unsafe:name"))
    assert found == [unsafe]
    assert list(store._find_by_id("metadata-id")) == [metadata_only]
    assert store.read("unsafe:name")["content"] == "unsafe"

    delete_abs = tmp_path / "delete-abs.md"
    _write_memory(delete_abs, "delete", id="delete-abs", layer="project")
    store.delete(str(delete_abs))
    assert not delete_abs.exists()
    delete_rel = tmp_path / "wiki" / "projects" / "delete-rel.md"
    _write_memory(delete_rel, "delete", id="delete-rel", layer="project")
    store.delete(str(delete_rel.relative_to(tmp_path)))
    assert not delete_rel.exists()
    with pytest.raises(FileNotFoundError):
        store.delete("missing")

    assert list(store.list_layer("missing")) == []
    assert list(store.list_layer("ephemeral")) == []
    run_file = tmp_path / ".agent" / "runs" / "run-1" / "scratch" / "run.md"
    _write_memory(run_file, "run", id="run", layer="ephemeral")
    (tmp_path / ".agent" / "runs" / "file").write_text("not dir", encoding="utf-8")
    assert list(store.list_layer("ephemeral")) == [run_file]

    session_file = tmp_path / ".agent" / "sessions" / "s" / "session.md"
    _write_memory(session_file, "session", id="session", layer="session")
    bad_session = tmp_path / ".agent" / "sessions" / "s" / "bad.md"
    bad_session.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert session_file in list(store.list_layer("session"))
    assert [item["id"] for item in store.iter_layer_items("session")] == ["session"]

    run_bad = tmp_path / ".agent" / "runs" / "run-1" / "scratch" / "bad.md"
    run_bad.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert [item["id"] for item in store.iter_layer_items("ephemeral")] == ["run"]

    transient = tmp_path / ".agent" / "transient" / "transient.md"
    _write_memory(transient, "transient", id="transient", layer="transient")
    transient_bad = tmp_path / ".agent" / "transient" / "bad.md"
    transient_bad.write_text("---\nid: [bad\n---\n", encoding="utf-8")
    assert [item["id"] for item in store.iter_layer_items("transient")] == ["transient"]

    collision = tmp_path / "wiki" / "projects" / "unsafe%3Aname.md"
    _write_memory(collision, "collision", id="collision", layer="project")
    dry_changes = store.migrate_unsafe_filenames(dry_run=True)
    assert any(change["id"] == "unsafe:name" and change["to"].endswith("-2.md") for change in dry_changes)
    changes = store.migrate_unsafe_filenames(dry_run=False)
    assert any(change["id"] == "unsafe:name" for change in changes)


def test_lifecycle_and_fts_edge_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    import core.fts as fts_mod
    from core.fts import FTSIndex
    from core.lifecycle import LifecycleAction, MemoryLifecycleManager, parse_timestamp

    assert parse_timestamp("") is None
    assert parse_timestamp("   ") is None
    assert parse_timestamp(datetime.now(timezone.utc)).tzinfo is not None
    assert parse_timestamp("not-a-date") is None

    now = datetime.now(timezone.utc)
    manager = MemoryLifecycleManager()
    retained = manager.plan_transition({"stage": "archived", "created_at": now.isoformat()}, now=now)
    inactive = manager.plan_transition(
        {
            "id": "inactive",
            "stage": "stored",
            "created_at": (now - timedelta(days=400)).isoformat(),
            "access_count": 0,
            "quality_score": 0.8,
        },
        now=now,
    )
    assert retained.action is LifecycleAction.RETAIN
    assert inactive.action is LifecycleAction.ARCHIVE

    fts = FTSIndex(db_path=str(tmp_path / "fts.db"))
    assert fts.search("   ") == []
    fts.index_item("bad-meta", "agent crew run", metadata={"layer": "project"})
    fts.index_item("dup", "agent crew run", metadata={"layer": "project"})
    fts.index_item("dup", "agent crew run", metadata={"layer": "project"})
    assert fts.search("agent-crew", limit=5)
    with fts._connect() as conn:
        conn.execute(
            "INSERT INTO items_fts (item_id, content, metadata) VALUES (?, ?, ?)",
            ("bad-json", "agent metadata", "{bad"),
        )
        conn.commit()
    assert fts.search("agent metadata", limit=5)[0]["metadata"] == {}
    monkeypatch.setattr(fts_mod, "expand_query", lambda query: [" ", query])
    assert fts.search("agent", limit=5)
    monkeypatch.setattr(fts_mod, "_sanitise_fts_variant", lambda variant: '"unterminated')
    assert fts.search("agent", limit=5) == []


class FakePolicy:
    def __init__(self) -> None:
        self.raise_check = False

    def check_promotion_eligible(self, item: dict[str, Any]) -> bool:
        if self.raise_check:
            raise RuntimeError("policy failed")
        return bool(item.get("eligible"))

    def get_next_layer(self, layer: str) -> str | None:
        return None if layer == "global" else "global"


class FakeGatewayStore:
    def __init__(self) -> None:
        self.items = [
            {"id": "match", "_path": "/tmp/match.md", "content_hash": "hash", "layer": "project", "tags": ["keep"], "content": "content"},
            {"id": "", "_path": "/tmp/noid.md", "content_hash": "hash", "layer": "project", "tags": [], "content": "content"},
        ]
        self.fail_iter_layers: set[str] = {"working"}
        self.fail_update = False

    def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
        if layer in self.fail_iter_layers:
            raise RuntimeError("iter failed")
        return list(self.items)

    def read(self, item_id: str) -> dict[str, Any]:
        if item_id == "missing":
            raise RuntimeError("read failed")
        return {"id": item_id, "_path": f"/tmp/{item_id}.md", "layer": "project", "tags": ["keep"], "content": "memory content", "access_count": 0}

    def update(self, path: str, metadata_updates: dict[str, Any]) -> None:
        if self.fail_update:
            raise RuntimeError("update failed")


class FakeLogger:
    def append(self, *args: Any, **kwargs: Any) -> None:
        return None


class FakeFTS:
    def index_item(self, **kwargs: Any) -> None:
        return None


class FakeSearch:
    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"content": "no id"},
            {"item_id": "missing", "content": "missing"},
            {"item_id": "hit", "content": "hit"},
        ]


class FakeObs:
    def log_search(self, **kwargs: Any) -> None:
        return None


def _gateway() -> Any:
    from core.gateway import MemoryGateway

    gw = MemoryGateway.__new__(MemoryGateway)
    gw._policy = FakePolicy()
    gw._store = FakeGatewayStore()
    gw._logger = FakeLogger()
    gw._fts = FakeFTS()
    gw._search = FakeSearch()
    gw._obs = FakeObs()
    gw._session_id = "session"
    gw._last_search_diagnostics = {}
    gw.promoted: list[tuple[str, str | None]] = []

    def promote(item_id: str, target_layer: str | None = None, **kwargs: Any) -> str:
        gw.promoted.append((item_id, target_layer))
        if item_id == "boom":
            raise RuntimeError("promote failed")
        return item_id

    gw.promote = promote
    return gw


def test_gateway_best_effort_helpers_and_search_filter_edges() -> None:
    gw = _gateway()

    gw._auto_promote_if_eligible("not-eligible", {"layer": "project", "eligible": False})
    gw._auto_promote_if_eligible("top", {"layer": "global", "eligible": True})
    gw._auto_promote_if_eligible("ok", {"layer": "project", "eligible": True})
    gw._policy.raise_check = True
    gw._auto_promote_if_eligible("ignored", {"layer": "project", "eligible": True})
    assert ("ok", "global") in gw.promoted

    assert gw._find_existing_by_hash("hash") == ("match", "project")
    assert gw._find_existing_by_hash("missing") is None

    gw._store.fail_update = True
    assert gw.auto_classify(item_id="missing", content="No pattern") == ["general"]
    assert gw.auto_classify(item_id="hit", content="Implemented tests and bug fix for regression") != []

    gw._store.fail_update = False
    assert gw.search("query", limit=3)
    results = gw.search("query", tags=["keep"], limit=1)
    assert [result["item_id"] for result in results] == ["hit"]
    gw._store.fail_update = True
    assert gw.search("query", tags=["keep"], limit=2)

    gw._policy.raise_check = False
    gw._store.fail_iter_layers.clear()
    gw._store.items.append({"id": "top2", "_path": "/tmp/top2.md", "layer": "global", "eligible": True})
    gw._store.items.append({"id": "boom", "_path": "/tmp/boom.md", "layer": "project", "eligible": True})
    assert gw.consolidate() >= 0

    gw._store.items.append({"layer": "project"})
    listed = gw.list_all()
    assert listed
    gw._store.fail_update = False
    assert gw.use("hit")["id"] == "hit"


def test_gateway_repo_resolution_and_obsidian_config_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.gateway as gateway
    from core.gateway import MemoryGateway, _resolve_repo_root

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path / "invalid"))
    with pytest.raises(FileNotFoundError):
        _resolve_repo_root()

    monkeypatch.delenv("MNEMOS_REPO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gateway, "__file__", str(tmp_path / "pkg" / "core" / "gateway.py"))
    with pytest.raises(FileNotFoundError):
        _resolve_repo_root()

    repo = tmp_path / "repo"
    (repo / "wiki").mkdir(parents=True)
    (repo / "wiki" / "policy.yaml").write_text("layers: {}\n", encoding="utf-8")
    monkeypatch.setenv("MNEMOS_BACKEND", "obsidian")
    monkeypatch.delenv("MNEMOS_VAULT_PATH", raising=False)
    with pytest.raises(ValueError):
        MemoryGateway(repo_root=str(repo))
