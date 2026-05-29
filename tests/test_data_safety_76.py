"""Data-safety scenario tests for issue #76.

Validation-only coverage (audit + gap fill) for the four acceptance criteria of
issue #76:

1. Memory corruption recovery is tested.
2. Partial data-loss scenarios are tested.
3. Archive / expire / promote lifecycle transitions are proven not to risk
   unintended loss.
4. These tests live under ``tests/`` and run by default pytest (AC4 is satisfied
   structurally by this file existing and passing).

This is **validation** work, not a redesign: every assertion checks the
*documented* engine contract. ``core/*`` is unchanged. The gap matrix mapping
each AC to existing coverage and to the scenarios below lives in the task's
``context/audit-76-gap.md``. Related operator docs: ``docs/remote-sync.md`` and
``docs/backup-restore.md``.

If a scenario had surfaced a real engine bug it would carry
``@pytest.mark.xfail(reason=..., strict=False)`` and a ``# TODO(#76)`` comment;
none did, so the file is xfail-free.
"""
from __future__ import annotations

import datetime
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Shared fixtures and helpers (mirrors the proven pattern in tests/test_bg.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal, offline mnemos repo under ``tmp_path``.

    Layer set covers transient → session → project → global so the lifecycle
    scenarios have a real promotion chain to exercise.
    """
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "transient"]:
        (agent / d).mkdir(parents=True)

    policy = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")
    return tmp_path


def _write_memory(
    path: Path,
    *,
    item_id: str = "test-item",
    layer: str = "project",
    stage: str = "stored",
    quality_score: float = 0.8,
    access_count: int = 0,
    created_at: str | None = None,
    content: str = "Test memory content",
) -> Path:
    """Drop a well-formed memory file with YAML front-matter.

    Bypasses the gateway/policy round-trip so a scenario can pre-condition exact
    filesystem state. ``Path.write_text`` closes its handle immediately, keeping
    the suite clean under ``filterwarnings = ["error"]`` (no ResourceWarning).
    """
    if created_at is None:
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    front_matter = (
        f"---\n"
        f"id: {item_id}\n"
        f"layer: {layer}\n"
        f"stage: {stage}\n"
        f"quality_score: {quality_score}\n"
        f"access_count: {access_count}\n"
        f"created_at: '{created_at}'\n"
        f"---\n"
        f"{content}\n"
    )
    path.write_text(front_matter, encoding="utf-8")
    return path


# ===========================================================================
# AC1 — Memory corruption recovery
# ===========================================================================

class TestCorruptionRecovery:
    """A corrupt on-disk file must never crash the store.

    Contract (``core/store.py``):
      * ``iter_layer_items`` wraps ``_parse_file`` in try/except and ``continue``s
        on any parse error — a corrupt file is silently skipped.
      * ``list_layer`` only ``glob("*.md")`` and never parses — so the corrupt
        file's *path* is still discoverable.
    """

    def test_malformed_yaml_frontmatter_is_skipped_not_crashed(self, repo_root: Path):
        """A file with malformed YAML front-matter is skipped by the parsing
        iterator while a sibling well-formed item is still yielded — and the
        non-parsing path listing still surfaces the corrupt file."""
        from core.store import MemoryStore

        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "good.md", item_id="good", content="Healthy item")
        # Malformed YAML: ``bad: : : value`` makes frontmatter.load raise
        # yaml.ScannerError, which iter_layer_items swallows.
        (proj / "broken.md").write_text(
            "---\nid: broken\nbad: : : value\nlayer: project\n---\nCorrupt body\n",
            encoding="utf-8",
        )

        store = MemoryStore(repo_root=str(repo_root))

        # iter_layer_items must NOT raise and must skip the corrupt file.
        parsed_ids = {item.get("id") for item in store.iter_layer_items("project")}
        assert "good" in parsed_ids
        assert "broken" not in parsed_ids  # corrupt file skipped, not surfaced

        # list_layer never parses → corrupt path is still discoverable.
        listed = {p.name for p in store.list_layer("project")}
        assert listed == {"good.md", "broken.md"}

    def test_truncated_frontmatter_does_not_crash_store(self, repo_root: Path):
        """A file truncated mid-front-matter (no closing ``---`` fence) does not
        crash the store. frontmatter parses it degenerately (empty metadata, no
        ``id``), so it is not a clean item — but iter_layer_items never raises
        and list_layer still yields its path."""
        from core.store import MemoryStore

        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "intact.md", item_id="intact", content="Intact item")
        # Truncated before the closing fence — simulates a write interrupted
        # mid-flush. No ScannerError; parses with empty metadata.
        (proj / "cut.md").write_text(
            "---\nid: cut\nlayer: project\nquality_sco",
            encoding="utf-8",
        )

        store = MemoryStore(repo_root=str(repo_root))

        # The store keeps operating: iteration completes without an exception.
        parsed_ids = {item.get("id") for item in store.iter_layer_items("project")}
        # The intact item is still readable...
        assert "intact" in parsed_ids
        # ...and the truncated file never produces a clean ``id`` (degenerate).
        assert "cut" not in parsed_ids

        # The path remains discoverable via the non-parsing listing.
        listed = {p.name for p in store.list_layer("project")}
        assert listed == {"intact.md", "cut.md"}


# ===========================================================================
# AC2 — Partial data-loss scenarios
# ===========================================================================

class TestPartialLoss:
    """Out-of-band filesystem mutation must degrade gracefully, never crash."""

    def test_out_of_band_file_deletion_does_not_raise(self, repo_root: Path):
        """Deleting one item file behind the store's back must not raise on the
        next list or iterate; the surviving item is unaffected."""
        from core.store import MemoryStore

        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "keep.md", item_id="keep", content="Keep me")
        _write_memory(proj / "vanish.md", item_id="vanish", content="Delete me")

        store = MemoryStore(repo_root=str(repo_root))

        # Out-of-band deletion (e.g. user rm, sync prune).
        (proj / "vanish.md").unlink()

        # Neither call raises; only the surviving item is reported.
        listed = {p.name for p in store.list_layer("project")}
        assert listed == {"keep.md"}
        parsed_ids = {item.get("id") for item in store.iter_layer_items("project")}
        assert parsed_ids == {"keep"}

    def test_whole_layer_directory_deletion_then_rewrite(self, repo_root: Path):
        """Deleting an entire layer directory yields an empty layer (no crash),
        and a subsequent ``store.write`` re-creates the directory and persists
        the new item."""
        from core.store import MemoryStore

        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "doomed.md", item_id="doomed", content="Pre-loss item")

        store = MemoryStore(repo_root=str(repo_root))

        # Wholesale layer loss (e.g. disk corruption, accidental rmtree).
        shutil.rmtree(proj)
        assert not proj.exists()

        # list_layer on a missing directory returns empty, never raises.
        assert list(store.list_layer("project")) == []
        assert list(store.iter_layer_items("project")) == []

        # store.write uses mkdir(parents=True, exist_ok=True) → dir is re-created.
        written = store.write(
            layer="project",
            item_id="recreated",
            content="Post-loss item",
            metadata={"layer": "project", "stage": "stored"},
        )
        assert written.exists()
        assert proj.exists()
        assert {p.name for p in store.list_layer("project")} == {written.name}

    def test_agent_ephemeral_wipe_keeps_wiki_data(self, repo_root: Path):
        """Wiping the ephemeral ``.agent/`` tree (sessions, runs, transient) must
        leave the persistent ``wiki/`` promotion data fully intact and the store
        operating."""
        from core.store import MemoryStore

        # Persistent promotion data in wiki/.
        _write_memory(
            repo_root / "wiki" / "projects" / "persist.md",
            item_id="persist",
            layer="project",
            content="Promoted, persistent knowledge",
        )
        # Ephemeral data under .agent/.
        _write_memory(
            repo_root / ".agent" / "sessions" / "s1" / "scratch.md",
            item_id="scratch",
            layer="session",
            content="Throwaway session note",
        )

        store = MemoryStore(repo_root=str(repo_root))

        # Ephemeral wipe (e.g. tmpfs cleared on reboot, manual .agent/ purge).
        shutil.rmtree(repo_root / ".agent")
        assert not (repo_root / ".agent").exists()

        # The store keeps operating: persistent project data is intact...
        proj_ids = {item.get("id") for item in store.iter_layer_items("project")}
        assert proj_ids == {"persist"}
        # ...and the lost session layer reports empty without raising.
        assert list(store.list_layer("session")) == []

        # A fresh write into a wiped dynamic layer succeeds (dir re-created).
        rewritten = store.write(
            layer="session",
            item_id="fresh-session",
            content="New session note after wipe",
            metadata={"layer": "session", "stage": "stored"},
            session_id="s2",
        )
        assert rewritten.exists()


# ===========================================================================
# AC3 — Lifecycle transition atomicity (archive / expire / promote)
# ===========================================================================

class TestLifecycleAtomicity:
    """Lifecycle transitions must never lose an item, even when they fail."""

    def test_promote_dest_write_failure_keeps_source(self, repo_root: Path):
        """``promote`` writes the destination *before* deleting the source. If
        the destination write fails, the exception propagates and the source
        item survives — the destination stays empty. No item is lost."""
        from core.gateway import MemoryGateway

        gateway = MemoryGateway(repo_root=str(repo_root))
        item_id = gateway.capture(
            layer="session",
            content="Atomic-promote source content (must survive a failed promote)",
            session_id="s1",
            no_classify=True,
        )
        assert item_id is not None

        session_dir = repo_root / ".agent" / "sessions" / "s1"
        project_dir = repo_root / "wiki" / "projects"
        assert len(list(session_dir.glob("*.md"))) == 1
        assert list(project_dir.glob("*.md")) == []

        # Inject a destination (project-layer) write failure. The ephemeral
        # session write already happened during capture; only the promote's
        # project write should explode.
        real_write = gateway._store.write

        def _failing_write(layer, item_id, content, metadata, **kwargs):
            if layer == "project":
                raise OSError("injected destination write failure")
            return real_write(layer, item_id, content, metadata, **kwargs)

        with patch.object(gateway._store, "write", side_effect=_failing_write):
            with pytest.raises(OSError, match="injected destination write failure"):
                gateway.promote(item_id, target_layer="project", force=True)

        # Source survived; destination never received the item → no loss.
        assert len(list(session_dir.glob("*.md"))) == 1
        assert list(project_dir.glob("*.md")) == []

    def test_archived_item_survives_gc_run(self, repo_root: Path):
        """A garbage-collectable item is *archived*, not destroyed:
        ``gc.run(dry_run=False)`` only flips ``stage`` to ``archived`` via
        ``store.update`` — it never unlinks the file."""
        import frontmatter

        from core.gc import GarbageCollector

        old_ts = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(hours=500)
        ).isoformat()
        target = _write_memory(
            repo_root / "wiki" / "projects" / "stale.md",
            item_id="stale",
            layer="project",
            stage="stored",
            quality_score=0.05,
            access_count=0,
            created_at=old_ts,
            content="Stale, low-quality, archivable item",
        )

        gc = GarbageCollector(
            repo_root=str(repo_root),
            gc_threshold=0.0,
            staleness_hours=0.01,
            limit=10,
        )
        report = gc.run(dry_run=False)

        assert report.archived >= 1
        # The file is NOT destroyed — it remains on disk...
        assert target.exists()
        # ...with its stage flipped to "archived" (soft-delete, recoverable).
        assert frontmatter.load(str(target)).metadata.get("stage") == "archived"

    def test_bg_chain_conserves_item_count(self, repo_root: Path, tmp_path: Path):
        """``bg.run_background_check(force=True)`` runs gc + auto-promote + dedup.
        A single distinct session item is conserved across the chain: it may be
        promoted session→project, but the union of all layers still holds exactly
        one item — none is lost."""
        from core.bg import run_background_check
        from core.store import MemoryStore

        # One item with unique content so cross-layer dedup cannot collapse it.
        _write_memory(
            repo_root / ".agent" / "sessions" / "s1" / "sole.md",
            item_id="sole",
            layer="session",
            content="Distinct conservation content that must not be lost",
        )

        ts_file = tmp_path / "bg.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), force=True)
        assert result.ran is True

        store = MemoryStore(repo_root=str(repo_root))
        all_layers = ["transient", "session", "project", "global",
                      "entities", "claims", "topics"]
        union = [p for layer in all_layers for p in store.list_layer(layer)]
        # Exactly one item survives across the whole layer union → conserved.
        assert len(union) == 1

    def test_transient_item_never_escapes_to_project(self, repo_root: Path, tmp_path: Path):
        """Regression: a transient (ephemeral, TTL-bound) item must never escape
        directly into the persistent ``wiki/projects/`` layer after a background
        pass. It stays in ``.agent/transient/``."""
        from core.bg import run_background_check
        from core.store import MemoryStore

        _write_memory(
            repo_root / ".agent" / "transient" / "ttl.md",
            item_id="ttl",
            layer="transient",
            content="Ephemeral transient content with a short TTL",
        )

        ts_file = tmp_path / "bg.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), force=True)
        assert result.ran is True

        store = MemoryStore(repo_root=str(repo_root))
        transient_paths = {p.name for p in store.list_layer("transient")}
        project_paths = {p.name for p in store.list_layer("project")}

        # The transient item is still in its own layer...
        assert "ttl.md" in transient_paths
        # ...and has NOT escaped into the persistent project layer.
        assert "ttl.md" not in project_paths
        assert project_paths == set()
