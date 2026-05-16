"""Tests for the observability layer (core/observability.py)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def obs_root(tmp_path):
    """Return a tmp repo root with wiki/ pre-created."""
    (tmp_path / "wiki").mkdir()
    return tmp_path


@pytest.fixture
def obs(obs_root):
    """Return an ObservabilityLogger pointed at tmp root."""
    from core.observability import ObservabilityLogger
    return ObservabilityLogger(repo_root=str(obs_root))


def _wait_for_writes(obs, timeout: float = 2.0) -> None:
    """Wait until async background writes have landed in the log file."""
    import threading
    # Join all non-main daemon threads (the background writer threads).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.daemon and t != threading.main_thread() and t.is_alive()]
        if not alive:
            break
        time.sleep(0.05)
    # Give a tiny extra window for the file write to flush.
    time.sleep(0.05)


class TestObservabilityLoggerInit:
    def test_creates_log_file_on_init(self, obs_root):
        from core.observability import ObservabilityLogger
        logger = ObservabilityLogger(repo_root=str(obs_root))
        assert (obs_root / "wiki" / "observability.jsonl").exists()

    def test_creates_parent_dirs_if_missing(self, tmp_path):
        from core.observability import ObservabilityLogger
        # wiki dir does NOT exist yet
        logger = ObservabilityLogger(repo_root=str(tmp_path))
        assert (tmp_path / "wiki" / "observability.jsonl").exists()


class TestLogCapture:
    def test_capture_event_is_written(self, obs, obs_root):
        obs.log_capture(
            memory_id="abc-123",
            layer="session",
            tags=["test"],
            session_id="sess-1",
            agent="claude",
        )
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        assert len(lines) >= 1
        entry = json.loads(lines[-1])
        assert entry["event"] == "capture"
        assert entry["memory_id"] == "abc-123"
        assert entry["layer"] == "session"
        assert entry["tags"] == ["test"]
        assert entry["session_id"] == "sess-1"
        assert entry["agent"] == "claude"
        assert "ts" in entry

    def test_capture_defaults(self, obs, obs_root):
        obs.log_capture(memory_id="xyz", layer="global")
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["tags"] == []
        assert entry["session_id"] == ""
        assert entry["agent"] == "claude"


class TestLogSearch:
    def test_search_event_is_written(self, obs, obs_root):
        results = [{"item_id": "mem-1", "score": 0.9}, {"item_id": "mem-2", "score": 0.7}]
        obs.log_search(
            keywords=["mnemos", "memory"],
            results=results,
            session_id="sess-2",
        )
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event"] == "search"
        assert entry["keywords"] == ["mnemos", "memory"]
        assert entry["result_count"] == 2
        assert len(entry["results"]) == 2
        assert entry["results"][0]["id"] == "mem-1"

    def test_hook_search_event(self, obs, obs_root):
        obs.log_hook_search(
            keywords=["hook", "test"],
            results=[],
            session_id="sess-3",
        )
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event"] == "hook_search"
        assert entry["result_count"] == 0


class TestLogGC:
    def test_gc_event_is_written(self, obs, obs_root):
        obs.log_gc(archived_count=5, dry_run=False, layers=["ephemeral"])
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event"] == "gc"
        assert entry["archived_count"] == 5
        assert entry["dry_run"] is False
        assert entry["layers"] == ["ephemeral"]

    def test_gc_dry_run_event(self, obs, obs_root):
        obs.log_gc(archived_count=0, dry_run=True)
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["dry_run"] is True


class TestLogPromotion:
    def test_promotion_event_is_written(self, obs, obs_root):
        obs.log_promotion(
            memory_id="mem-promo",
            from_layer="session",
            to_layer="project",
        )
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event"] == "promotion"
        assert entry["memory_id"] == "mem-promo"
        assert entry["from_layer"] == "session"
        assert entry["layer"] == "project"


class TestLogSessionStart:
    def test_session_start_event(self, obs, obs_root):
        obs.log_hook_session_start(
            session_id="sess-start",
            agent="claude",
            memory_count=12,
        )
        _wait_for_writes(obs)
        lines = (obs_root / "wiki" / "observability.jsonl").read_text().splitlines()
        entry = json.loads(lines[-1])
        assert entry["event"] == "hook_session_start"
        assert entry["memory_count"] == 12
        assert entry["session_id"] == "sess-start"


class TestReadEntries:
    def _seed(self, obs, obs_root, n_entries: int = 5) -> None:
        """Write n_entries to the log synchronously (not async)."""
        log_path = obs_root / "wiki" / "observability.jsonl"
        for i in range(n_entries):
            entry = {
                "ts": f"2026-05-{16 + (i % 3):02d}T10:00:00Z",
                "event": "capture" if i % 2 == 0 else "hook_search",
                "agent": "claude",
                "session_id": f"sess-{i % 2}",
                "memory_id": f"mem-{i}",
                "keywords": [f"kw{i}"],
                "result_count": i,
                "results": [],
            }
            with log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")

    def test_read_all_entries(self, obs, obs_root):
        self._seed(obs, obs_root, 5)
        entries = obs.read_entries()
        assert len(entries) == 5

    def test_read_tail(self, obs, obs_root):
        self._seed(obs, obs_root, 10)
        entries = obs.read_entries(tail=3)
        assert len(entries) == 3

    def test_filter_by_session(self, obs, obs_root):
        self._seed(obs, obs_root, 5)
        entries = obs.read_entries(session_id="sess-0")
        for e in entries:
            assert e["session_id"] == "sess-0"

    def test_filter_by_event(self, obs, obs_root):
        self._seed(obs, obs_root, 5)
        entries = obs.read_entries(events=["capture"])
        for e in entries:
            assert e["event"] == "capture"

    def test_empty_log_returns_empty_list(self, obs):
        entries = obs.read_entries()
        assert entries == []


class TestAggregateStats:
    def _seed_stats(self, obs_root) -> None:
        """Write known entries for stats aggregation testing."""
        log_path = obs_root / "wiki" / "observability.jsonl"
        entries = [
            {"ts": "2026-05-14T10:00:00Z", "event": "capture", "agent": "claude",
             "session_id": "s1", "layer": "project", "memory_id": "m1", "tags": []},
            {"ts": "2026-05-14T10:01:00Z", "event": "capture", "agent": "claude",
             "session_id": "s1", "layer": "global", "memory_id": "m2", "tags": []},
            {"ts": "2026-05-14T10:02:00Z", "event": "hook_search", "agent": "claude",
             "session_id": "s1", "keywords": ["mnemos", "memory"],
             "results": [{"id": "m1", "score": 0.9}], "result_count": 1},
            {"ts": "2026-05-15T11:00:00Z", "event": "hook_search", "agent": "claude",
             "session_id": "s2", "keywords": ["mnemos"],
             "results": [{"id": "m1", "score": 0.8}, {"id": "m2", "score": 0.6}], "result_count": 2},
            {"ts": "2026-05-10T08:00:00Z", "event": "gc", "agent": "unknown",
             "session_id": "", "archived_count": 3, "dry_run": False, "layers": []},
        ]
        for e in entries:
            with log_path.open("a") as f:
                f.write(json.dumps(e) + "\n")

    def test_stats_structure(self, obs, obs_root):
        self._seed_stats(obs_root)
        stats = obs.aggregate_stats(days=30)
        assert "captures_by_layer" in stats
        assert "searches_per_day" in stats
        assert "top_keywords" in stats
        assert "top_surfaced_memories" in stats
        assert "last_gc_ts" in stats
        assert "last_gc_count" in stats
        assert "hook_calls" in stats
        assert "total_entries" in stats

    def test_captures_aggregated_by_layer(self, obs, obs_root):
        self._seed_stats(obs_root)
        stats = obs.aggregate_stats(days=30)
        assert stats["captures_by_layer"].get("project", 0) >= 1
        assert stats["captures_by_layer"].get("global", 0) >= 1

    def test_top_keywords_ranked(self, obs, obs_root):
        self._seed_stats(obs_root)
        stats = obs.aggregate_stats(days=30)
        # "mnemos" appears in 2 searches, should be at top
        kw_dict = dict(stats["top_keywords"])
        assert "mnemos" in kw_dict
        assert kw_dict["mnemos"] >= 2

    def test_top_surfaced_memories(self, obs, obs_root):
        self._seed_stats(obs_root)
        stats = obs.aggregate_stats(days=30)
        # m1 surfaced in 2 search results
        mem_dict = dict(stats["top_surfaced_memories"])
        assert "m1" in mem_dict
        assert mem_dict["m1"] >= 2

    def test_last_gc_tracked(self, obs, obs_root):
        self._seed_stats(obs_root)
        stats = obs.aggregate_stats(days=30)
        assert stats["last_gc_ts"] is not None
        assert stats["last_gc_count"] == 3

    def test_empty_log_returns_zero_stats(self, obs):
        stats = obs.aggregate_stats(days=7)
        assert stats["total_entries"] == 0
        assert stats["hook_calls"] == 0
        assert stats["captures_by_layer"] == {}


class TestBriefStats:
    def test_brief_stats_returns_string(self, obs, obs_root):
        summary = obs.brief_stats()
        assert isinstance(summary, str)

    def test_brief_stats_non_empty_with_data(self, obs, obs_root):
        # Seed one capture
        log_path = obs_root / "wiki" / "observability.jsonl"
        entry = {"ts": "2026-05-15T10:00:00Z", "event": "capture", "agent": "claude",
                 "session_id": "s1", "layer": "project", "memory_id": "m1", "tags": []}
        with log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        summary = obs.brief_stats()
        assert "captures" in summary.lower() or "memories" in summary.lower()


class TestGatewayIntegration:
    """Integration: observability events emitted by MemoryGateway operations."""

    @pytest.fixture
    def repo_root(self, tmp_path):
        import yaml
        wiki = tmp_path / "wiki"
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (wiki / d).mkdir(parents=True)
        agent = tmp_path / ".agent"
        for d in ["runs", "sessions", "state", "reports", "tools"]:
            (agent / d).mkdir(parents=True)
        (agent / "workflows" / "hooks").mkdir(parents=True)
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

    def test_capture_writes_observability_event(self, repo_root):
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        gw.capture(layer="global", content="observability test", run_id="run-obs")
        _wait_for_writes(gw.observability)
        obs_path = repo_root / "wiki" / "observability.jsonl"
        assert obs_path.exists()
        lines = [l for l in obs_path.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1
        events = [json.loads(l)["event"] for l in lines]
        assert "capture" in events

    def test_search_writes_observability_event(self, repo_root):
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        gw.capture(layer="global", content="searchable unique token xyz", run_id="run-obs")
        gw.search("searchable unique token xyz")
        _wait_for_writes(gw.observability)
        obs_path = repo_root / "wiki" / "observability.jsonl"
        lines = [l for l in obs_path.read_text().splitlines() if l.strip()]
        events = [json.loads(l)["event"] for l in lines]
        assert "search" in events

    def test_promote_writes_observability_event(self, repo_root):
        from core.gateway import MemoryGateway
        gw = MemoryGateway(repo_root=str(repo_root))
        item_id = gw.capture(layer="project", content="to be promoted obs test", run_id="run-obs")
        gw.promote(item_id=item_id, run_id="run-obs")
        _wait_for_writes(gw.observability)
        obs_path = repo_root / "wiki" / "observability.jsonl"
        lines = [l for l in obs_path.read_text().splitlines() if l.strip()]
        events = [json.loads(l)["event"] for l in lines]
        assert "promotion" in events

    def test_observability_property_returns_logger(self, repo_root):
        from core.gateway import MemoryGateway
        from core.observability import ObservabilityLogger
        gw = MemoryGateway(repo_root=str(repo_root))
        assert isinstance(gw.observability, ObservabilityLogger)
