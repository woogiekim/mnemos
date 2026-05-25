"""CLI edge-path coverage using isolated fakes."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

import core.cli as cli_mod
from core.policy import PolicyViolationError


class DictNamespace(SimpleNamespace):
    def to_dict(self) -> dict[str, Any]:
        return dict(getattr(self, "payload", self.__dict__))


class FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.items = {
            "ok": {"id": "ok", "layer": "project", "content": "body", "_path": str(root / "ok.md")},
        }

    def read(self, item_id: str) -> dict[str, Any]:
        if item_id == "missing" or item_id == "json-item":
            raise FileNotFoundError(item_id)
        return dict(self.items.get(item_id, self.items["ok"]))

    def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
        if layer == "topics":
            raise RuntimeError("cannot iterate")
        return [
            {"id": "", "content": "no id"},
            {"id": "tagged", "tags": ["existing"], "content": "tagged"},
            {"id": f"{layer}-new", "tags": [], "content": "new"},
        ]


class FakePolicy:
    def get_next_layer(self, current_layer: str) -> str:
        return "global" if current_layer == "project" else "project"


class FakeObservability:
    def __init__(self, root: Path) -> None:
        self._log_path = root / "wiki" / "observability.jsonl"
        self.entries: list[dict[str, Any]] = []
        self.stats_payload: dict[str, Any] = {
            "captures_by_layer": {},
            "searches_per_day": {},
            "hook_calls": 0,
            "top_keywords": [],
            "top_surfaced_memories": [],
            "last_gc_ts": "",
            "last_gc_count": 0,
            "total_entries": 0,
        }

    def read_entries(
        self,
        tail: int = 20,
        session_id: str | None = None,
        events: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        entries = self.entries
        if session_id:
            entries = [entry for entry in entries if entry.get("session_id") == session_id]
        if events:
            entries = [entry for entry in entries if entry.get("event") in events]
        return entries[-tail:]

    def aggregate_stats(self, days: int) -> dict[str, Any]:
        return dict(self.stats_payload)


class FakeEventBus:
    def subscribe(self, event_name: str, handler: Any) -> None:
        return None


class FakeGateway:
    def __init__(self, root: Path) -> None:
        self._root = str(root)
        self._store = FakeStore(root)
        self._policy = FakePolicy()
        self.observability = FakeObservability(root)
        self.event_bus = FakeEventBus()
        self.last_capture_was_duplicate = False
        self.last_search_diagnostics = {"partial_failure": False}
        self.capture_exc: Exception | None = None
        self.search_exc: Exception | None = None
        self.classify_exc: Exception | None = None
        self.demote_exc: Exception | None = None
        self.archive_exc: Exception | None = None
        self.consolidate_exc: Exception | None = None

    def capture(self, **kwargs: Any) -> str | None:
        if self.capture_exc:
            raise self.capture_exc
        return kwargs.get("item_id") or "json-item"

    def read(self, item_id: str) -> dict[str, Any]:
        return self._store.read(item_id)

    def use(self, item_id: str) -> None:
        if item_id == "missing":
            raise FileNotFoundError(item_id)

    def update(self, item_id: str, content: str) -> None:
        if item_id == "missing":
            raise FileNotFoundError(item_id)

    def classify(self, item_id: str, tag: str, layer: str | None = None) -> None:
        if self.classify_exc:
            raise self.classify_exc

    def auto_classify(self, item_id: str, content: str) -> list[str]:
        return ["auto"] if item_id.endswith("new") else []

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        if self.search_exc:
            raise self.search_exc
        return [{"item_id": "result", "content": "search body", "source": "fake"}]

    def promote(self, **kwargs: Any) -> str:
        return kwargs["item_id"]

    def demote(self, **kwargs: Any) -> None:
        if self.demote_exc:
            raise self.demote_exc

    def archive(self, item_id: str) -> None:
        if self.archive_exc:
            raise self.archive_exc

    def forget(self, item_id: str) -> None:
        if item_id == "missing":
            raise FileNotFoundError(item_id)

    def delete(self, item_id: str) -> None:
        if item_id == "missing":
            raise FileNotFoundError(item_id)

    def consolidate(self, dry_run: bool = False) -> int:
        if self.consolidate_exc:
            raise self.consolidate_exc
        return 2

    def log(self, **kwargs: Any) -> None:
        return None


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CliRunner, FakeGateway]:
    gateway = FakeGateway(tmp_path)
    for rel in ["wiki/global", "wiki/projects", "wiki/entities", "wiki/claims", "wiki/topics"]:
        (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli_mod, "_get_gateway", lambda: gateway)
    return CliRunner(), gateway


def test_capture_classify_search_and_context_error_edges(
    fake_cli: tuple[CliRunner, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, gateway = fake_cli

    repo_root = Path(gateway._root) / "bootstrap-src"
    (repo_root / "core").mkdir(parents=True)
    original_resolve = Path.resolve

    def flaky_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name == "core":
            raise OSError("resolve failed")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)
    monkeypatch.setattr(cli_mod.shutil, "copytree", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("copy failed")))
    cli_mod._bootstrap_sync_source(str(repo_root))

    result = runner.invoke(cli_mod.cli, ["capture", "--content", "json", "--json"])
    assert result.exit_code == 0
    assert '"item": null' in result.output

    gateway.capture_exc = PolicyViolationError("blocked")
    result = runner.invoke(cli_mod.cli, ["capture", "--content", "json", "--json"])
    assert result.exit_code == 1
    assert "policy_violation" in result.output

    gateway.capture_exc = RuntimeError("backend failed")
    result = runner.invoke(cli_mod.cli, ["capture", "--content", "json", "--json"])
    assert result.exit_code == 1
    assert "backend_error" in result.output
    gateway.capture_exc = None

    result = runner.invoke(cli_mod.cli, ["classify", "--all", "--untagged", "--dry-run"])
    assert result.exit_code == 0
    assert "would classify" in result.output
    assert "skipped" in result.output

    gateway.classify_exc = FileNotFoundError("nope")
    assert runner.invoke(cli_mod.cli, ["classify", "nope", "--tag", "x"]).exit_code == 1
    gateway.classify_exc = PolicyViolationError("bad")
    assert runner.invoke(cli_mod.cli, ["classify", "nope", "--tag", "x"]).exit_code == 1
    gateway.classify_exc = None

    gateway.search_exc = RuntimeError("search down")
    result = runner.invoke(cli_mod.cli, ["search", "q"])
    assert result.exit_code == 1
    assert "search down" in result.output

    result = runner.invoke(cli_mod.cli, ["context", "--json", "--render", "--prompt", "q"])
    assert result.exit_code != 0
    result = runner.invoke(cli_mod.cli, ["context", "--prompt", "q"])
    assert result.exit_code != 0


def test_read_use_edit_promote_demote_archive_forget_edges(
    fake_cli: tuple[CliRunner, FakeGateway],
) -> None:
    runner, gateway = fake_cli

    assert '"content": "body"' in runner.invoke(cli_mod.cli, ["read", "ok"]).output
    assert runner.invoke(cli_mod.cli, ["read", "missing"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["read", "missing", "--json"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["use", "ok"]).exit_code == 0
    assert runner.invoke(cli_mod.cli, ["use", "missing"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["edit", "ok", "--content", "new"]).exit_code == 0
    assert runner.invoke(cli_mod.cli, ["edit", "missing", "--content", "new"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["promote", "--quiet", "missing"]).exit_code == 1

    result = runner.invoke(cli_mod.cli, ["demote", "ok", "--target-layer", "session"])
    assert result.exit_code == 0
    gateway.demote_exc = PolicyViolationError("blocked")
    assert runner.invoke(cli_mod.cli, ["demote", "ok", "--target-layer", "session"]).exit_code == 1
    gateway.demote_exc = FileNotFoundError("missing")
    assert runner.invoke(cli_mod.cli, ["demote", "ok", "--target-layer", "session"]).exit_code == 1
    gateway.demote_exc = None

    gateway.archive_exc = PolicyViolationError("blocked")
    assert runner.invoke(cli_mod.cli, ["archive", "ok"]).exit_code == 1
    gateway.archive_exc = FileNotFoundError("missing")
    assert runner.invoke(cli_mod.cli, ["archive", "ok"]).exit_code == 1
    gateway.archive_exc = None

    assert runner.invoke(cli_mod.cli, ["forget", "ok"], input="y\n").exit_code == 0
    assert runner.invoke(cli_mod.cli, ["forget", "--force", "missing"]).exit_code == 1


def test_transcript_daemon_capabilities_version_consolidate_and_log_edges(
    fake_cli: tuple[CliRunner, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, gateway = fake_cli
    transcript = Path(gateway._root) / "transcript.jsonl"
    transcript.write_text("{}", encoding="utf-8")

    import core.transcript as transcript_mod

    monkeypatch.setattr(
        transcript_mod,
        "capture_transcript",
        lambda **kwargs: {"captured_count": 1, "duplicate_count": 2, "skipped_count": 3},
    )
    result = runner.invoke(cli_mod.cli, ["capture-transcript", "--transcript-path", str(transcript)])
    assert result.exit_code == 0
    assert "captured=1" in result.output

    monkeypatch.setattr(transcript_mod, "capture_transcript", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad")))
    assert runner.invoke(cli_mod.cli, ["capture-transcript", "--transcript-path", str(transcript)]).exit_code == 1

    calls: list[tuple[str, bool]] = []
    import core.daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "manage_autonomous_daemon", lambda action, as_json=False: calls.append((action, as_json)))
    for action in ["run", "status", "install", "uninstall"]:
        assert runner.invoke(cli_mod.cli, ["daemon", action, "--json"]).exit_code == 0
    assert calls == [(action, True) for action in ["run", "status", "install", "uninstall"]]

    assert "capture_json" in runner.invoke(cli_mod.cli, ["capabilities"]).output
    assert runner.invoke(cli_mod.cli, ["version"]).exit_code == 0

    result = runner.invoke(cli_mod.cli, ["consolidate", "--dry-run"])
    assert result.exit_code == 0
    assert "Would promote 2" in result.output
    gateway.consolidate_exc = RuntimeError("broken")
    assert runner.invoke(cli_mod.cli, ["consolidate"]).exit_code == 1
    gateway.consolidate_exc = None

    gateway.observability.entries = [
        {"ts": "2026-01-01T00:00:00Z", "event": "hook_search", "agent": "a", "keywords": ["k"], "result_count": 1},
        {"ts": "2026-01-01T00:00:01Z", "event": "hook_session_start", "agent": "a", "session_id": "s", "memory_count": 2},
        {"ts": "2026-01-01T00:00:02Z", "event": "gc", "agent": "a", "archived_count": 3, "dry_run": True},
        {"ts": "2026-01-01T00:00:03Z", "event": "promotion", "agent": "a", "memory_id": "m", "from_layer": "a", "layer": "b"},
        {"ts": "2026-01-01T00:00:04Z", "event": "hook_post_tool", "agent": "a", "tool_name": "Edit", "session_id": "s"},
        {"ts": "2026-01-01T00:00:05Z", "event": "other", "agent": "a", "x": "y"},
    ]
    assert runner.invoke(cli_mod.cli, ["log"]).exit_code == 0
    assert runner.invoke(cli_mod.cli, ["log", "--op", "x", "--id", "y", "--layer", "z", "--meta", "{bad"]).exit_code == 1


class FakeEngine:
    fail: bool = False

    def __init__(self, gateway: Any, suppress_backend_sync: bool = False) -> None:
        self.gateway = gateway
        self.suppress_backend_sync = suppress_backend_sync

    def _maybe_fail(self) -> None:
        if self.fail:
            raise RuntimeError("engine failed")

    def run_lifecycle(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        item = DictNamespace(item_id="life", layer="project", action="summarize", applied=False, error="boom", reason="why")
        return DictNamespace(
            dry_run=kwargs["dry_run"],
            evaluated_count=1,
            planned_count=1,
            applied_count=0,
            failed_count=1,
            items=[item],
            payload={"status": "dry_run", "items": ["life"]},
        )

    def record_lifecycle_report(self, report: Any) -> Path:
        return Path("/tmp/lifecycle.json")

    def compute_metrics(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        return DictNamespace(
            scores={"context_continuity_score": 0.9},
            item_count=1,
            issue_count=2,
            payload={"scores": {"context_continuity_score": 0.9}, "item_count": 1, "issue_count": 2},
        )

    def record_metrics_snapshot(self, metrics: Any) -> Path:
        return Path("/tmp/metrics.json")

    def retrieval_backend_health(self) -> DictNamespace:
        self._maybe_fail()
        return DictNamespace(
            status="degraded",
            retrieval_contract="fallback",
            backends=[{"name": "fts", "status": "ok", "configured": True, "available": True, "reason": "detail"}],
            payload={"status": "degraded"},
        )

    def record_backend_health_report(self, report: Any) -> Path:
        return Path("/tmp/backends.json")

    def audit_readiness(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        gap = DictNamespace(severity="high", code="gap", message="missing", remediation="fix")
        return DictNamespace(
            status="not_ready",
            ready=False,
            metrics=DictNamespace(item_count=1),
            backend_health=DictNamespace(status="degraded"),
            gaps=[gap],
            payload={"ready": False},
        )

    def record_readiness_report(self, report: Any) -> Path:
        return Path("/tmp/readiness.json")

    def run_compression_job(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        page = DictNamespace(
            artifact_id="page",
            source_item_ids=["a", "b"],
            estimated_tokens=3,
            applied=False,
            error="failed",
        )
        return DictNamespace(
            dry_run=kwargs["dry_run"],
            input_count=2,
            page_count=1,
            applied_count=0,
            failed_count=1,
            pages=[page],
            payload={"page_count": 1},
        )

    def record_compression_report(self, report: Any) -> Path:
        return Path("/tmp/compress.json")

    def validate_health(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        gate = DictNamespace(name="continuity", passed=False, actual=0.1, threshold=0.9)
        return DictNamespace(status="fail", gates=[gate], passed=False, payload={"passed": False})

    def record_validation_report(self, report: Any) -> Path:
        return Path("/tmp/validation.json")

    def calibrate_health(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        calibration = DictNamespace(name="continuity", baseline=0.8, threshold=0.75)
        return DictNamespace(
            status="calibrated",
            sample_count=1,
            strategy="empirical",
            calibrations=[calibration],
            payload={"status": "calibrated"},
        )

    def record_calibration_report(self, report: Any) -> Path:
        return Path("/tmp/calibration.json")

    def recover_store(self, **kwargs: Any) -> DictNamespace:
        self._maybe_fail()
        issue = DictNamespace(code="missing-stage", path="/tmp/item.md", repaired=True)
        return DictNamespace(
            dry_run=kwargs["dry_run"],
            scanned_count=1,
            readable_count=1,
            corrupt_count=1,
            repaired_count=1,
            reindexed_count=1,
            issues=[issue],
            payload={"repaired_count": 1},
        )

    def record_recovery_report(self, report: Any) -> Path:
        return Path("/tmp/recovery.json")


def test_memory_os_cli_commands_success_and_error_paths(
    fake_cli: tuple[CliRunner, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, _gateway = fake_cli
    import core.operations as operations_mod

    monkeypatch.setattr(operations_mod, "MemoryOperationsEngine", FakeEngine)
    FakeEngine.fail = False

    commands = [
        ["lifecycle-run", "--record"],
        ["memory-metrics", "--record"],
        ["memory-backends", "--record"],
        ["memory-readiness", "--record"],
        ["memory-compress", "--record"],
        ["memory-validate", "--record"],
        ["memory-calibrate", "--record"],
        ["recover", "--record"],
    ]
    for command in commands:
        result = runner.invoke(cli_mod.cli, command)
        expected_exit = 1 if command[0] in {"memory-readiness", "memory-validate"} else 0
        assert result.exit_code == expected_exit, (command, result.output)

    json_commands = [
        ["lifecycle-run", "--json", "--record"],
        ["memory-metrics", "--json", "--record"],
        ["memory-backends", "--json", "--record"],
        ["memory-readiness", "--json", "--record"],
        ["memory-compress", "--json", "--record"],
        ["memory-validate", "--json", "--record"],
        ["memory-calibrate", "--json", "--record"],
        ["recover", "--json", "--record"],
    ]
    for command in json_commands:
        expected_exit = 1 if command[0] in {"memory-readiness", "memory-validate"} else 0
        assert runner.invoke(cli_mod.cli, command).exit_code == expected_exit

    FakeEngine.fail = True
    for command in [["lifecycle-run"], ["memory-metrics"], ["memory-backends"], ["memory-readiness"], ["memory-compress"], ["memory-validate"], ["memory-calibrate"], ["recover"]]:
        assert runner.invoke(cli_mod.cli, command).exit_code == 1
    for command in [["lifecycle-run", "--json"], ["memory-metrics", "--json"], ["memory-backends", "--json"], ["memory-readiness", "--json"], ["memory-compress", "--json"], ["memory-validate", "--json"], ["memory-calibrate", "--json"], ["recover", "--json"]]:
        assert runner.invoke(cli_mod.cli, command).exit_code == 1
    FakeEngine.fail = False


def test_gc_bg_audit_stats_sync_ingest_and_migrate_edges(
    fake_cli: tuple[CliRunner, FakeGateway],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner, gateway = fake_cli

    import core.bg as bg_mod
    import core.gc as gc_mod

    class FakeGcReport:
        archived = 1
        regions_processed = 2
        archived_items = [{"item_id": "gc-1", "score_breakdown": {"staleness": 1, "access": 0, "quality": 1, "stage": 1}}]

        def summary_lines(self) -> list[str]:
            return ["gc summary"]

    class FakeGarbageCollector:
        fail = False

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, dry_run: bool = False) -> FakeGcReport:
            if self.fail:
                raise RuntimeError("gc failed")
            return FakeGcReport()

    monkeypatch.setattr(gc_mod, "GarbageCollector", FakeGarbageCollector)
    result = runner.invoke(cli_mod.cli, ["gc", "--verbose"])
    assert result.exit_code == 0
    assert "Score breakdowns" in result.output
    assert runner.invoke(cli_mod.cli, ["gc", "--json"]).exit_code == 0
    FakeGarbageCollector.fail = True
    assert runner.invoke(cli_mod.cli, ["gc"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["gc", "--json"]).exit_code == 1
    FakeGarbageCollector.fail = False

    bg_result = DictNamespace(
        ran=True,
        has_activity=False,
        elapsed_ms=12,
        memory_os_enabled=True,
        memory_os_health_status=None,
        memory_os_readiness_status=None,
        memory_os_evidence_paths=["a", "b"],
        to_context_block=lambda: "<mnemos-context />",
    )
    monkeypatch.setattr(bg_mod, "run_background_check", lambda **kwargs: bg_result)
    result = runner.invoke(cli_mod.cli, ["bg-check", "--force", "--verbose", "--memory-os"])
    assert result.exit_code == 0
    assert "Memory OS" in result.output

    assert runner.invoke(cli_mod.cli, ["audit"]).exit_code == 0
    gateway.observability.entries = [
        {"ts": "2026-01-01T00:00:00Z", "event": "hook_search", "agent": "a", "keywords": ["k"], "result_count": 1},
        {"ts": "2026-01-01T00:00:01Z", "event": "hook_session_start", "agent": "a", "session_id": "s", "memory_count": 2},
        {"ts": "2026-01-01T00:00:02Z", "event": "gc", "agent": "a", "archived_count": 3, "dry_run": True},
        {"ts": "2026-01-01T00:00:03Z", "event": "promotion", "agent": "a", "memory_id": "m", "from_layer": "a", "layer": "b"},
        {"ts": "2026-01-01T00:00:04Z", "event": "hook_post_tool", "agent": "a", "tool_name": "Edit", "session_id": "s"},
        {"ts": "2026-01-01T00:00:05Z", "event": "other", "agent": "a", "x": "y"},
    ]
    assert runner.invoke(cli_mod.cli, ["audit"]).exit_code == 0
    gateway.observability.entries = []

    topics_dir = Path(gateway._root) / "wiki" / "topics"
    topics_dir.rmdir()
    assert runner.invoke(cli_mod.cli, ["stats"]).exit_code == 0

    gateway.observability.stats_payload = {
        "captures_by_layer": {"project": 2},
        "searches_per_day": {"2026-01-01": 3},
        "hook_calls": 3,
        "top_keywords": [("memory", 2)],
        "top_surfaced_memories": [("mem-1", 4)],
        "last_gc_ts": "2026-01-02T00:00:00Z",
        "last_gc_count": 1,
        "total_entries": 7,
    }
    result = runner.invoke(cli_mod.cli, ["stats"])
    assert result.exit_code == 0
    assert "Usage Dashboard" in result.output

    class FakeBackend:
        def __init__(self) -> None:
            self.fail: str | Exception | None = None

        def sync_pull(self) -> None:
            if self.fail:
                if isinstance(self.fail, Exception):
                    raise self.fail
                raise RuntimeError(self.fail)

        def sync_push(self) -> None:
            if self.fail:
                if isinstance(self.fail, Exception):
                    raise self.fail
                raise RuntimeError(self.fail)

        def sync_status(self) -> dict[str, Any]:
            if self.fail:
                if isinstance(self.fail, Exception):
                    raise self.fail
                raise RuntimeError(self.fail)
            return {"branch": "main", "last_pull_ts": 0.0, "last_push_ts": 1.0}

        def sync_continue(self) -> None:
            if self.fail:
                if isinstance(self.fail, Exception):
                    raise self.fail
                raise RuntimeError(self.fail)

    backend = FakeBackend()
    monkeypatch.setattr(cli_mod, "_get_obsidian_backend", lambda: backend)
    for command in [["sync", "pull"], ["sync", "push"], ["sync", "status"], ["sync", "continue"]]:
        assert runner.invoke(cli_mod.cli, command).exit_code == 0
    from core.obsidian import SyncConflictError

    backend.fail = SyncConflictError("conflicted")
    assert runner.invoke(cli_mod.cli, ["sync", "pull"]).exit_code == 1
    assert runner.invoke(cli_mod.cli, ["sync", "continue"]).exit_code == 1
    backend.fail = "sync failed"
    for command in [["sync", "pull"], ["sync", "push"], ["sync", "status"], ["sync", "continue"]]:
        assert runner.invoke(cli_mod.cli, command).exit_code == 1
    backend.fail = None

    import agents.ingest as ingest_mod
    import agents.scanner as scanner_mod

    class FakeScanner:
        def __init__(self, project_root: Path) -> None:
            self.project_root = project_root

        def discover(self) -> list[tuple[Path, str, str]]:
            return [(tmp_path / "CLAUDE.md", "project", "project")]

        def discover_memory_files(self) -> list[tuple[Path, str, str]]:
            return [(tmp_path / "memory.md", "global", "claude_memory")]

    class FakeIngestAgent:
        fail: Exception | None = None

        def __init__(self, gateway: Any) -> None:
            self.gateway = gateway

        def run_scanner_results_dedup(self, *args: Any, **kwargs: Any) -> dict[str, list[str]]:
            if self.fail:
                raise self.fail
            if kwargs.get("dry_run"):
                return {"created": ["new.md"], "updated": ["changed.md"], "skipped": ["same.md"]}
            return {"created": ["new-id"], "updated": ["changed-id"], "skipped": ["same-id"]}

    monkeypatch.setattr(scanner_mod, "ClaudeMdScanner", FakeScanner)
    monkeypatch.setattr(ingest_mod, "IngestAgent", FakeIngestAgent)
    result = runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "would ingest" in result.output
    result = runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path)])
    assert result.exit_code == 0
    assert "memory-sync" in result.output
    FakeIngestAgent.fail = PolicyViolationError("blocked")
    assert runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path)]).exit_code == 1
    FakeIngestAgent.fail = RuntimeError("broken")
    assert runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path)]).exit_code == 1
    FakeIngestAgent.fail = None

    class FakeScannerMemoryOnly(FakeScanner):
        def discover(self) -> list[tuple[Path, str, str]]:
            return []

    monkeypatch.setattr(scanner_mod, "ClaudeMdScanner", FakeScannerMemoryOnly)
    FakeIngestAgent.fail = PolicyViolationError("blocked")
    assert runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path)]).exit_code == 1
    FakeIngestAgent.fail = RuntimeError("broken")
    assert runner.invoke(cli_mod.cli, ["ingest-claude-md", "--project-root", str(tmp_path)]).exit_code == 1
    FakeIngestAgent.fail = None

    import core.store as store_mod
    import core.obsidian as obsidian_mod

    class FakeMemoryStore:
        def __init__(self, repo_root: str) -> None:
            self.repo_root = repo_root

        def migrate_unsafe_filenames(self, dry_run: bool = False) -> list[dict[str, str]]:
            return [{"from": "old:name.md", "to": "old%3Aname.md"}]

        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            if layer == "global":
                raise RuntimeError("skip source layer")
            return [
                {"id": "", "content": "no id"},
                {"id": "migrate-me", "content": "new content", "layer": layer, "_path": "x"},
                {"id": "skip-me", "content": "same content", "layer": layer, "_path": "x"},
            ]

    class FakeObsidianBackend:
        stats = {
            "renamed": 1,
            "skipped": 1,
            "renames": [
                {"layer": "project", "old_name": "uuid.md", "new_name": "slug.md", "reason": "renamed"},
                {"layer": "project", "old_name": "slug.md", "new_name": "slug.md", "reason": "skipped"},
            ],
        }

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.writes: list[dict[str, Any]] = []

        def rename_uuid_to_slug(self, dry_run: bool = False, commit: bool = False) -> dict[str, Any]:
            return dict(self.stats)

        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            if layer == "global":
                raise RuntimeError("skip destination layer")
            import hashlib
            import re
            import unicodedata

            content = "same content"
            src_hash = hashlib.sha256(
                re.sub(r"\s+", " ", unicodedata.normalize("NFKC", content).strip()).lower().encode("utf-8")
            ).hexdigest()
            return [{"id": "skip-me", "content_hash": src_hash}]

        def write(self, **kwargs: Any) -> None:
            if kwargs["item_id"] == "migrate-me":
                raise RuntimeError("write failed")
            self.writes.append(kwargs)

    monkeypatch.setattr(store_mod, "MemoryStore", FakeMemoryStore)
    monkeypatch.setattr(obsidian_mod, "ObsidianBackend", FakeObsidianBackend)
    monkeypatch.setattr(obsidian_mod, "OBSIDIAN_LAYERS", ["project", "global"])
    result = runner.invoke(cli_mod.cli, ["migrate", "--safe-filenames", "--dry-run"])
    assert result.exit_code == 0
    assert "unsafe filename" in result.output

    assert runner.invoke(cli_mod.cli, ["migrate", "--uuid-to-slug"]).exit_code != 0
    result = runner.invoke(cli_mod.cli, ["migrate", "--uuid-to-slug", "--vault-path", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0
    assert "would be skipped" in result.output
    result = runner.invoke(cli_mod.cli, ["migrate", "--uuid-to-slug", "--vault-path", str(tmp_path)])
    assert result.exit_code == 0
    assert "Renamed 1" in result.output

    assert runner.invoke(cli_mod.cli, ["migrate"]).exit_code != 0
    assert runner.invoke(cli_mod.cli, ["migrate", "--from", "default"]).exit_code != 0
    assert runner.invoke(cli_mod.cli, ["migrate", "--from", "default", "--to", "obsidian"]).exit_code != 0
    result = runner.invoke(
        cli_mod.cli,
        ["migrate", "--from", "default", "--to", "obsidian", "--vault-path", str(tmp_path), "--dry-run"],
    )
    assert result.exit_code == 0
    assert "already up-to-date" in result.output
    result = runner.invoke(
        cli_mod.cli,
        ["migrate", "--from", "default", "--to", "obsidian", "--vault-path", str(tmp_path)],
    )
    assert result.exit_code == 0
    assert "Migrated" in result.output

    import core.config as config_mod
    import core.git as git_mod

    cfg_default = DictNamespace(backend="default", vault_path=None, sync=DictNamespace(remote="origin"))
    monkeypatch.setattr(config_mod, "get_backend_config", lambda repo_root: cfg_default)
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 1

    cfg_obsidian = DictNamespace(backend="obsidian", vault_path=str(tmp_path), sync=DictNamespace(remote="upstream"))
    monkeypatch.setattr(config_mod, "get_backend_config", lambda repo_root: cfg_obsidian)
    git_calls: list[str] = []
    monkeypatch.setattr(git_mod, "init", lambda vault_path: git_calls.append("init"))
    monkeypatch.setattr(git_mod, "set_remote", lambda vault_path, remote, url: git_calls.append("remote"))
    monkeypatch.setattr(git_mod, "fetch", lambda vault_path, remote: git_calls.append("fetch"))
    monkeypatch.setattr(git_mod, "set_upstream", lambda vault_path, remote, branch: git_calls.append("upstream"))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 0

    monkeypatch.setattr(git_mod, "fetch", lambda vault_path, remote: (_ for _ in ()).throw(git_mod.GitCommandError(1, "fetch failed")))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 0
    monkeypatch.setattr(git_mod, "fetch", lambda vault_path, remote: None)
    monkeypatch.setattr(git_mod, "set_upstream", lambda vault_path, remote, branch: (_ for _ in ()).throw(git_mod.GitCommandError(1, "upstream failed")))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 0
    monkeypatch.setattr(git_mod, "init", lambda vault_path: (_ for _ in ()).throw(git_mod.GitNotFoundError("no git")))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 1
    monkeypatch.setattr(git_mod, "init", lambda vault_path: (_ for _ in ()).throw(git_mod.GitCommandError(1, "git failed")))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 1
    monkeypatch.setattr(git_mod, "init", lambda vault_path: (_ for _ in ()).throw(RuntimeError("generic")))
    assert runner.invoke(cli_mod.cli, ["sync", "init", "--remote", "git@example/repo.git"]).exit_code == 1
