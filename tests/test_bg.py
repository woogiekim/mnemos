"""Tests for the background operations engine (core/bg.py)."""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Shared fixture: minimal mnemos repo
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal mnemos repo structure."""
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state"]:
        (agent / d).mkdir(parents=True)

    policy = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
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
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


def _write_memory(
    path: Path,
    *,
    item_id: str = "test-item",
    layer: str = "ephemeral",
    stage: str = "stored",
    quality_score: float = 0.8,
    access_count: int = 0,
    created_at: str | None = None,
    content: str = "Test memory content",
) -> Path:
    """Write a minimal memory file with YAML front-matter."""
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
    path.write_text(front_matter)
    return path


# ---------------------------------------------------------------------------
# Tests: throttle helpers (should_run, touch_timestamp)
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_should_run_when_no_timestamp(self, tmp_path: Path):
        from core.bg import should_run, _timestamp_path
        with patch("core.bg._timestamp_path", return_value=tmp_path / "no-such-file.ts"):
            assert should_run(interval_minutes=5) is True

    def test_should_run_when_fresh_timestamp(self, tmp_path: Path):
        """Fresh timestamp (just touched) → should NOT run yet."""
        from core.bg import should_run, _timestamp_path
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()
        with patch("core.bg._timestamp_path", return_value=ts_file):
            assert should_run(interval_minutes=5) is False

    def test_should_run_when_old_timestamp(self, tmp_path: Path):
        """Old timestamp (mtime in the past) → should run."""
        import time as _time
        from core.bg import should_run
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()
        # Set mtime to 10 minutes ago
        old_mtime = _time.time() - 600
        os.utime(str(ts_file), (old_mtime, old_mtime))
        with patch("core.bg._timestamp_path", return_value=ts_file):
            assert should_run(interval_minutes=5) is True

    def test_touch_timestamp_creates_file(self, tmp_path: Path):
        from core.bg import touch_timestamp
        ts_file = tmp_path / "touch-test.ts"
        assert not ts_file.exists()
        with patch("core.bg._timestamp_path", return_value=ts_file):
            touch_timestamp()
        assert ts_file.exists()

    def test_touch_timestamp_updates_mtime(self, tmp_path: Path):
        import time as _time
        from core.bg import touch_timestamp
        ts_file = tmp_path / "touch-test.ts"
        ts_file.touch()
        # Back-date the file
        old_mtime = _time.time() - 600
        os.utime(str(ts_file), (old_mtime, old_mtime))
        with patch("core.bg._timestamp_path", return_value=ts_file):
            touch_timestamp()
        assert ts_file.stat().st_mtime > old_mtime


# ---------------------------------------------------------------------------
# Tests: content hashing / normalisation
# ---------------------------------------------------------------------------

class TestContentHash:
    def test_identical_content_same_hash(self):
        from core.bg import _content_hash
        assert _content_hash("hello world") == _content_hash("hello world")

    def test_whitespace_variations_same_hash(self):
        from core.bg import _content_hash
        # Extra spaces, different case → same hash after normalisation
        h1 = _content_hash("Hello   World")
        h2 = _content_hash("hello world")
        assert h1 == h2

    def test_different_content_different_hash(self):
        from core.bg import _content_hash
        assert _content_hash("foo") != _content_hash("bar")

    def test_hash_is_hex_string(self):
        from core.bg import _content_hash
        h = _content_hash("some content")
        assert re.match(r"^[0-9a-f]{64}$", h)

    def test_nfkc_fullwidth_ascii_same_hash(self):
        """Full-width Unicode letters must normalise to ASCII equivalents via NFKC.

        NFKC normalisation is applied as the first step in _normalise_content
        (before whitespace/case folding) so that compatibility variants are
        collapsed.  This is the same contract required by gateway.capture()
        dedup (Issue #4 anti-regression).
        """
        from core.bg import _content_hash
        ascii_form = "decision about architecture"
        # Full-width Latin letters (U+FF41..U+FF5A) → ASCII via NFKC
        fullwidth_form = "ｄｅｃｉｓｉｏｎ ａｂｏｕｔ ａｒｃｈｉｔｅｃｔｕｒｅ"
        assert _content_hash(ascii_form) == _content_hash(fullwidth_form), (
            "NFKC should collapse full-width letters to ASCII equivalents, "
            "producing the same normalised form and therefore the same hash"
        )

    def test_nfkc_ligature_normalisation(self):
        """Unicode ligatures (e.g. ﬁ U+FB01) must be expanded by NFKC."""
        from core.bg import _content_hash
        # ﬁ (U+FB01 LATIN SMALL LIGATURE FI) NFKC-normalises to "fi"
        with_ligature = "ﬁle system"
        without_ligature = "file system"
        assert _content_hash(with_ligature) == _content_hash(without_ligature), (
            "NFKC should expand the ﬁ ligature to 'fi', producing the same hash"
        )


# ---------------------------------------------------------------------------
# Tests: find_duplicates
# ---------------------------------------------------------------------------

class TestFindDuplicates:
    def test_no_duplicates_in_empty_repo(self, repo_root: Path):
        from core.bg import find_duplicates
        groups = find_duplicates(str(repo_root))
        assert groups == []

    def test_detects_identical_content(self, repo_root: Path):
        """Two items with same content in project layer → one duplicate group."""
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "item-a.md", item_id="item-a", layer="project",
                      content="Identical memory content")
        _write_memory(proj / "item-b.md", item_id="item-b", layer="project",
                      content="Identical memory content")

        from core.bg import find_duplicates
        groups = find_duplicates(str(repo_root), layers=["project"])
        assert len(groups) == 1
        item_ids = {i["item_id"] for i in groups[0].items}
        assert "item-a" in item_ids
        assert "item-b" in item_ids

    def test_whitespace_variations_detected_as_duplicate(self, repo_root: Path):
        """Content that differs only in whitespace/case is a duplicate."""
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "item-a.md", item_id="item-a", layer="project",
                      content="Same content here")
        _write_memory(proj / "item-b.md", item_id="item-b", layer="project",
                      content="  same content   here  ")

        from core.bg import find_duplicates
        groups = find_duplicates(str(repo_root), layers=["project"])
        assert len(groups) == 1

    def test_different_content_not_grouped(self, repo_root: Path):
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "item-a.md", item_id="item-a", layer="project",
                      content="First unique memory")
        _write_memory(proj / "item-b.md", item_id="item-b", layer="project",
                      content="Second unique memory")

        from core.bg import find_duplicates
        groups = find_duplicates(str(repo_root), layers=["project"])
        assert groups == []

    def test_primary_is_highest_layer(self, repo_root: Path):
        """When duplicates exist in different layers, primary is from the highest layer."""
        session_dir = repo_root / ".agent" / "sessions" / "sess-1"
        proj = repo_root / "wiki" / "projects"
        _write_memory(session_dir / "item-s.md", item_id="item-s", layer="session",
                      content="Shared content")
        _write_memory(proj / "item-p.md", item_id="item-p", layer="project",
                      content="Shared content")

        from core.bg import find_duplicates
        groups = find_duplicates(str(repo_root), layers=["session", "project"])
        assert len(groups) == 1
        # project > session in LAYER_ORDER
        assert groups[0].primary["layer"] == "project"

    def test_duplicate_group_attributes(self, repo_root: Path):
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "item-a.md", item_id="item-a", layer="project",
                      content="Same content")
        _write_memory(proj / "item-b.md", item_id="item-b", layer="project",
                      content="Same content")

        from core.bg import find_duplicates, DuplicateGroup
        groups = find_duplicates(str(repo_root), layers=["project"])
        g = groups[0]
        assert isinstance(g, DuplicateGroup)
        assert len(g.content_hash) == 64  # SHA-256 hex
        assert len(g.items) == 2
        assert len(g.duplicates) == 1  # one duplicate (not the primary)


# ---------------------------------------------------------------------------
# Tests: DuplicateGroup
# ---------------------------------------------------------------------------

class TestDuplicateGroup:
    def _make_group(self, items: list[dict]) -> "DuplicateGroup":
        from core.bg import DuplicateGroup
        return DuplicateGroup(content_hash="abc123", items=items)

    def test_primary_selects_highest_layer(self):
        group = self._make_group([
            {"item_id": "a", "layer": "session"},
            {"item_id": "b", "layer": "project"},
            {"item_id": "c", "layer": "ephemeral"},
        ])
        assert group.primary["item_id"] == "b"  # project is highest

    def test_duplicates_excludes_primary(self):
        group = self._make_group([
            {"item_id": "a", "layer": "session"},
            {"item_id": "b", "layer": "project"},
        ])
        primary_id = group.primary["item_id"]
        for d in group.duplicates:
            assert d["item_id"] != primary_id


# ---------------------------------------------------------------------------
# Tests: BackgroundCheckResult
# ---------------------------------------------------------------------------

class TestBackgroundCheckResult:
    def test_has_activity_false_when_nothing(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, gc_archived=0, promoted=0)
        assert r.has_activity is False

    def test_has_activity_true_when_gc_archived(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, gc_archived=3)
        assert r.has_activity is True

    def test_has_activity_true_when_promoted(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, promoted=2)
        assert r.has_activity is True

    def test_to_context_block_empty_when_no_activity(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True)
        assert r.to_context_block() == ""

    def test_to_context_block_has_xml_wrapper(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, gc_archived=2, promoted=1)
        block = r.to_context_block()
        assert '<mnemos-context type="background-activity">' in block
        assert "</mnemos-context>" in block

    def test_to_context_block_mentions_gc(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, gc_archived=5)
        block = r.to_context_block()
        assert "GC archived 5" in block

    def test_to_context_block_mentions_promoted(self):
        from core.bg import BackgroundCheckResult
        r = BackgroundCheckResult(ran=True, promoted=3)
        block = r.to_context_block()
        assert "promoted 3" in block

    def test_to_context_block_mentions_duplicates(self):
        from core.bg import BackgroundCheckResult, DuplicateGroup
        group = DuplicateGroup(
            content_hash="abc",
            items=[
                {"item_id": "dup-a", "layer": "session"},
                {"item_id": "dup-b", "layer": "project"},
            ],
        )
        r = BackgroundCheckResult(ran=True, duplicate_groups=[group])
        block = r.to_context_block()
        assert "Duplicate detected" in block

    def test_to_context_block_caps_at_three_duplicate_groups(self):
        from core.bg import BackgroundCheckResult, DuplicateGroup

        def _group(h: str) -> DuplicateGroup:
            return DuplicateGroup(
                content_hash=h,
                items=[
                    {"item_id": f"{h}-a", "layer": "session"},
                    {"item_id": f"{h}-b", "layer": "session"},
                ],
            )

        groups = [_group(f"hash{i}") for i in range(5)]
        r = BackgroundCheckResult(ran=True, duplicate_groups=groups)
        block = r.to_context_block()
        assert "2 more duplicate group(s)" in block


# ---------------------------------------------------------------------------
# Tests: run_background_check — throttle behaviour
# ---------------------------------------------------------------------------

class TestRunBackgroundCheck:
    def test_not_run_when_throttled(self, repo_root: Path, tmp_path: Path):
        """When should_run returns False, result.ran is False."""
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()  # fresh — throttle should block
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), interval_minutes=5)
        assert result.ran is False

    def test_runs_when_forced(self, repo_root: Path, tmp_path: Path):
        """force=True bypasses throttle."""
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()  # fresh — would normally throttle
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), force=True)
        assert result.ran is True

    def test_runs_when_not_throttled(self, repo_root: Path, tmp_path: Path):
        """When timestamp is absent, the check runs."""
        from core.bg import run_background_check
        ts_file = tmp_path / "nonexistent.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), interval_minutes=5)
        assert result.ran is True

    def test_touches_timestamp_after_run(self, repo_root: Path, tmp_path: Path):
        """After a successful run, the timestamp file is updated."""
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        assert not ts_file.exists()
        with patch("core.bg._timestamp_path", return_value=ts_file):
            run_background_check(str(repo_root), force=True)
        assert ts_file.exists()

    def test_elapsed_ms_is_positive(self, repo_root: Path, tmp_path: Path):
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(str(repo_root), force=True)
        assert result.elapsed_ms >= 0.0

    def test_gc_archives_stale_items(self, repo_root: Path, tmp_path: Path):
        """Full check with a stale ephemeral item → gc_archived > 0."""
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "stale.md",
            item_id="stale",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_threshold=0.0,
                gc_staleness_hours=0.01,
                gc_limit=10,
                auto_promote_enabled=False,
                dedup_enabled=False,
            )
        assert result.ran is True
        assert result.gc_archived >= 1

    def test_dedup_detected_in_full_check(self, repo_root: Path, tmp_path: Path):
        """Full check with duplicate project items → duplicate_groups non-empty."""
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "dup-a.md", item_id="dup-a", layer="project",
                      content="Duplicate project memory")
        _write_memory(proj / "dup-b.md", item_id="dup-b", layer="project",
                      content="Duplicate project memory")

        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=True,
                dedup_layers=["project"],
            )
        assert result.ran is True
        assert len(result.duplicate_groups) >= 1

    def test_no_gc_flag_skips_gc(self, repo_root: Path, tmp_path: Path):
        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=False,
            )
        assert result.gc_archived == 0

    def test_no_dedup_flag_skips_dedup(self, repo_root: Path, tmp_path: Path):
        proj = repo_root / "wiki" / "projects"
        _write_memory(proj / "dup-a.md", item_id="dup-a", layer="project",
                      content="Same content again")
        _write_memory(proj / "dup-b.md", item_id="dup-b", layer="project",
                      content="Same content again")

        from core.bg import run_background_check
        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=False,
            )
        assert result.duplicate_groups == []


# ---------------------------------------------------------------------------
# Tests: CLI bg-check command
# ---------------------------------------------------------------------------

class TestBgCheckCliCommand:
    @pytest.fixture
    def runner(self):
        from click.testing import CliRunner
        return CliRunner()

    def test_bg_check_exits_zero_when_throttled(self, runner, repo_root: Path, monkeypatch, tmp_path):
        """When throttled, command exits 0 with no output."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()
        from core.cli import cli
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(cli, ["bg-check"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_bg_check_force_runs_and_exits_zero(self, runner, repo_root: Path, monkeypatch, tmp_path):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        ts_file = tmp_path / "ts.ts"
        from core.cli import cli
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(cli, ["bg-check", "--force"])
        assert result.exit_code == 0

    def test_bg_check_verbose_prints_summary(self, runner, repo_root: Path, monkeypatch, tmp_path):
        """With --verbose, even a no-activity run prints something."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        ts_file = tmp_path / "ts.ts"
        from core.cli import cli
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(cli, ["bg-check", "--force", "--verbose"])
        assert result.exit_code == 0
        assert "mnemos bg" in result.output

    def test_bg_check_emits_context_block_on_activity(self, runner, repo_root: Path, monkeypatch, tmp_path):
        """When GC archives something, a <mnemos-context> block appears in stdout."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        now = datetime.datetime.now(datetime.timezone.utc)
        old_ts = (now - datetime.timedelta(hours=500)).isoformat()
        scratch = repo_root / ".agent" / "runs" / "r1" / "scratch"
        _write_memory(
            scratch / "stale.md",
            item_id="stale",
            layer="ephemeral",
            stage="stored",
            quality_score=0.1,
            access_count=0,
            created_at=old_ts,
        )
        ts_file = tmp_path / "ts.ts"
        from core.cli import cli
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(
                cli,
                [
                    "bg-check",
                    "--force",
                    "--no-promote",
                    "--no-dedup",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert '<mnemos-context type="background-activity">' in result.output

    def test_bg_check_interval_zero_forces_run(self, runner, repo_root: Path, monkeypatch, tmp_path):
        """Passing --interval 0 forces a run even without --force."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        ts_file = tmp_path / "ts.ts"
        ts_file.touch()  # fresh — would normally throttle
        from core.cli import cli
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(cli, ["bg-check", "--interval", "0", "--verbose"])
        assert result.exit_code == 0
        assert "mnemos bg" in result.output

    def test_bg_check_memory_os_records_evidence(self, repo_root: Path, tmp_path):
        """Opt-in Memory OS phase records health evidence from background maintenance."""
        from core.bg import run_background_check
        from core.gateway import MemoryGateway

        gateway = MemoryGateway(repo_root=str(repo_root))
        gateway.capture(
            layer="project",
            item_id="bg-memory-os",
            content="Workflow-aware operational memory preserves historical continuity.",
            tags=["workflow", "continuity"],
            quality_score=0.96,
            extra_metadata={
                "trust_level": "verified",
                "workflow_id": "bg-memory-os",
                "access_count": 5,
            },
            no_classify=True,
        )

        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=False,
                memory_os_enabled=True,
                memory_os_min_score=0.7,
                memory_os_layers=["project"],
            )

        assert result.ran is True
        assert result.memory_os_enabled is True
        assert result.memory_os_validation_passed is True
        assert result.memory_os_health_status == "passed"
        assert result.memory_os_retrieval_status == "ok"
        assert result.memory_os_ready is True
        assert result.memory_os_readiness_status == "ready"
        assert len(result.memory_os_evidence_paths) == 5
        assert all(Path(path).exists() for path in result.memory_os_evidence_paths)
        assert (repo_root / ".agent" / "reports" / "memory-os" / "latest-backends.json").exists()
        assert (repo_root / ".agent" / "reports" / "memory-os" / "latest-health.json").exists()
        assert (repo_root / ".agent" / "reports" / "memory-os" / "latest-readiness.json").exists()
        assert (repo_root / ".agent" / "reports" / "memory-os" / "readiness-history.jsonl").exists()

    def test_bg_check_memory_os_recover_repairs_before_scoring(self, repo_root: Path, tmp_path):
        """Opt-in recovery repairs metadata and records recovery evidence before scoring."""
        from core.bg import run_background_check

        recoverable = repo_root / "wiki" / "projects" / "recoverable-bg.md"
        recoverable.write_text(
            "---\nlayer: project\n---\nRecoverable background memory.\n",
            encoding="utf-8",
        )

        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=False,
                memory_os_enabled=True,
                memory_os_recover=True,
                memory_os_min_score=0.0,
                memory_os_layers=["project"],
            )

        assert result.ran is True
        assert result.memory_os_repaired > 0
        assert result.memory_os_reindexed == 1
        assert result.memory_os_validation_passed is True
        assert result.memory_os_ready is True
        assert len(result.memory_os_evidence_paths) == 6
        assert (repo_root / ".agent" / "reports" / "memory-os" / "latest-recovery.json").exists()

        repaired_text = recoverable.read_text(encoding="utf-8")
        assert "id: recoverable-bg" in repaired_text
        assert "content_hash:" in repaired_text

    def test_bg_check_memory_os_quiet_records_without_output(
        self,
        runner,
        repo_root: Path,
        monkeypatch,
        tmp_path,
    ):
        """--quiet suppresses output while opt-in Memory OS evidence is still recorded."""
        from core.cli import cli
        from core.gateway import MemoryGateway

        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        MemoryGateway(repo_root=str(repo_root)).capture(
            layer="project",
            item_id="bg-cli-memory-os",
            content="Workflow-aware operational memory preserves historical continuity.",
            tags=["workflow", "continuity"],
            quality_score=0.96,
            extra_metadata={
                "trust_level": "verified",
                "workflow_id": "bg-cli-memory-os",
                "access_count": 5,
            },
            no_classify=True,
        )

        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = runner.invoke(
                cli,
                [
                    "bg-check",
                    "--force",
                    "--quiet",
                    "--no-gc",
                    "--no-promote",
                    "--no-dedup",
                    "--memory-os",
                    "--memory-os-min-score",
                    "0.7",
                    "--memory-os-layer",
                    "project",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert result.output == ""
        latest_metrics = repo_root / ".agent" / "reports" / "memory-os" / "latest-metrics.json"
        latest_backends = repo_root / ".agent" / "reports" / "memory-os" / "latest-backends.json"
        latest_readiness = repo_root / ".agent" / "reports" / "memory-os" / "latest-readiness.json"
        readiness_history = repo_root / ".agent" / "reports" / "memory-os" / "readiness-history.jsonl"
        assert latest_metrics.exists()
        assert latest_backends.exists()
        assert latest_readiness.exists()
        assert readiness_history.exists()
        payload = json.loads(latest_metrics.read_text(encoding="utf-8"))
        assert payload["kind"] == "metrics_snapshot"

    def test_bg_check_memory_os_readiness_failure_is_activity(
        self,
        repo_root: Path,
        tmp_path: Path,
        monkeypatch,
    ):
        """Autonomous readiness failures surface as background activity."""
        from core.bg import run_background_check

        monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "invalid_backend")

        ts_file = tmp_path / "ts.ts"
        with patch("core.bg._timestamp_path", return_value=ts_file):
            result = run_background_check(
                str(repo_root),
                force=True,
                gc_enabled=False,
                auto_promote_enabled=False,
                dedup_enabled=False,
                memory_os_enabled=True,
                memory_os_min_score=0.0,
                memory_os_record=True,
            )

        assert result.ran is True
        assert result.memory_os_ready is False
        assert result.memory_os_readiness_status == "not_ready"
        assert result.memory_os_readiness_gaps > 0
        assert result.has_activity is True
        assert "Memory OS readiness not_ready" in result.to_context_block()
        assert (repo_root / ".agent" / "reports" / "memory-os" / "latest-readiness.json").exists()
        assert (repo_root / ".agent" / "reports" / "memory-os" / "readiness-history.jsonl").exists()


# ---------------------------------------------------------------------------
# Tests: ClaudeCodeAdapter hook registration for bg-check
# ---------------------------------------------------------------------------

class TestClaudeAdapterBgHook:
    def test_install_registers_two_post_tool_use_entries(self, tmp_path: Path, monkeypatch):
        """After install, settings.json should have two PostToolUse hooks."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text('{"hooks": {}}')
        (claude_dir / "CLAUDE.md").write_text("# Test\n")

        adapter = ClaudeCodeAdapter()
        adapter.install(tmp_path)

        import json
        data = json.loads(settings.read_text())
        post_hooks = data["hooks"].get("PostToolUse", [])
        assert len(post_hooks) == 2, f"Expected 2 PostToolUse hooks, got {len(post_hooks)}"

    def test_install_registers_ingest_hook(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text('{"hooks": {}}')
        (claude_dir / "CLAUDE.md").write_text("")

        ClaudeCodeAdapter().install(tmp_path)

        import json
        data = json.loads(settings.read_text())
        post_hooks = data["hooks"].get("PostToolUse", [])
        cmds = " ".join(str(h) for h in post_hooks)
        assert "ingest-claude-md" in cmds

    def test_install_registers_bg_hook(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text('{"hooks": {}}')
        (claude_dir / "CLAUDE.md").write_text("")

        ClaudeCodeAdapter().install(tmp_path)

        import json
        data = json.loads(settings.read_text())
        post_hooks = data["hooks"].get("PostToolUse", [])
        cmds = " ".join(str(h) for h in post_hooks)
        assert "PostToolUse.sh" in cmds

    def test_verify_hooks_detects_missing_bg_hook(self, tmp_path: Path, monkeypatch):
        """verify_hooks reports missing when bg hook is absent."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter
        import json

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        # Only the ingest hook, not the bg-check hook
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "MNEMOS_REPO_ROOT=\"/x\" mnemos ingest-claude-md"}]},
                ],
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "MNEMOS_REPO_ROOT=\"/x\" bash \"/x/hooks/UserPromptSubmit.sh\" 2>/dev/null || true"}]},
                ],
            }
        }
        settings.write_text(json.dumps(data))
        (claude_dir / "CLAUDE.md").write_text("<!-- mnemos-start -->foo<!-- mnemos-end -->")

        ok, missing = ClaudeCodeAdapter().verify_hooks(tmp_path)
        assert not ok
        assert any("bg-check" in m for m in missing)

    def test_verify_hooks_passes_when_all_present(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text('{"hooks": {}}')
        (claude_dir / "CLAUDE.md").write_text("<!-- mnemos-start -->foo<!-- mnemos-end -->")

        ClaudeCodeAdapter().install(tmp_path)

        ok, missing = ClaudeCodeAdapter().verify_hooks(tmp_path)
        assert ok, f"Expected all hooks present but missing: {missing}"
        assert missing == []

    def test_idempotent_install(self, tmp_path: Path, monkeypatch):
        """Running install twice should not add duplicate hooks."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.adapters.claude import ClaudeCodeAdapter
        import json

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings = claude_dir / "settings.json"
        settings.write_text('{"hooks": {}}')
        (claude_dir / "CLAUDE.md").write_text("")

        adapter = ClaudeCodeAdapter()
        adapter.install(tmp_path)
        adapter.install(tmp_path)

        data = json.loads(settings.read_text())
        post_hooks = data["hooks"].get("PostToolUse", [])
        # Should still be exactly 2 (no duplicates from second install)
        assert len(post_hooks) == 2
