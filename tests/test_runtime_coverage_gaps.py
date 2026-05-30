"""Coverage for runtime edge paths in context, search, transcript, and daemon modules."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


def test_context_helpers_and_retrieval_edge_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    import core.context as context

    real_import = builtins.__import__

    def import_without_korean(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "core.korean":
            raise RuntimeError("no korean")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_korean)
    assert context.extract_keywords("CamelCase Camel Case and the", limit=5)
    monkeypatch.setattr(builtins, "__import__", real_import)

    assert context._score(0, 0) == 0.0
    assert context._parse_dt(datetime.now(timezone.utc)).tzinfo is not None
    assert context._parse_dt("not-a-date") is None
    assert context._freshness_score(None) == 0.45
    assert context._signal_density("", ["memory"]) == 0.0
    assert context._relevance_score({"content": ""}, [], 0.4) == 0.4

    item = {"created_at": "2000-01-01T00:00:00Z", "confidence": 0.1}
    assert context._skip_reason(0.1, item, "content", {"noise": 0.0, "freshness": 0.1, "confidence": 0.1}) == "stale"
    assert context._skip_reason(0.1, {}, "content", {"noise": 0.0, "freshness": 1.0, "confidence": 1.0}) == "low_signal"

    class FakeStore:
        def read(self, item_id: str) -> dict[str, Any]:
            if item_id == "missing":
                raise RuntimeError("missing")
            return {
                "id": item_id,
                "layer": "project",
                "content": "memory continuity signal",
                "quality_score": 0.9,
                "confidence": 0.9,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tags": ["memory"],
            }

    class FakeGateway:
        def __init__(self) -> None:
            self._root = tmp_path
            self._store = FakeStore()
            self.calls = 0
            self.last_search_diagnostics = {
                "partial_failure": True,
                "fallback_used": True,
                "degraded_reasons": ["fts"],
            }

        def search(self, query: str, limit: int) -> list[dict[str, Any]]:
            self.calls += 1
            if query == "raise":
                raise RuntimeError("search failed")
            if self.calls == 1:
                return []
            return [
                {"item_id": f"missing-{i}", "content": "fallback memory continuity signal", "metadata": {"layer": "project"}}
                for i in range(6)
            ]

    payload = context.retrieve_context(
        prompt="memory continuity",
        session_id="s",
        host="test",
        gateway=FakeGateway(),
        limit=1,
        max_chars=20,
    )
    assert payload["count"] == 1
    assert payload["selection"]["skipped_reasons"]["limit"] >= 1
    assert payload["status"] == "degraded"

    failing = context.retrieve_context(
        prompt="raise",
        session_id=None,
        host="test",
        gateway=FakeGateway(),
    )
    assert failing["partial_failure"] is True

    class BrokenGateway:
        def __init__(self) -> None:
            raise RuntimeError("cannot build")

    monkeypatch.setattr(context, "MemoryGateway", BrokenGateway)
    no_gateway = context.retrieve_context(prompt="q", session_id=None, host="test")
    assert no_gateway["status"] == "degraded"
    assert context.render_promotion_block(repo_root=None) == ""
    assert context.render_promotion_block(repo_root=str(tmp_path / "missing-root")) == ""

    promo_log = tmp_path / ".agent" / "observability.jsonl"
    promo_log.parent.mkdir(parents=True)
    promo_log.write_text(
        "\n".join(
            [
                    "",
                    "{bad json",
                json.dumps({"event": "capture", "ts": "2026-01-01T00:00:00Z"}),
                json.dumps({"event": "promotion", "ts": "2020-01-01T00:00:00Z", "memory_id": "old", "layer": "project"}),
                json.dumps({"event": "promotion", "ts": "2026-01-01T00:00:00Z", "memory_id": "new", "layer": "global"}),
            ]
        ),
        encoding="utf-8",
    )
    cursor = tmp_path / "cursor.txt"
    cursor.write_text("2021-01-01T00:00:00Z\n", encoding="utf-8")
    monkeypatch.setenv("MNEMOS_PROMO_CURSOR", str(cursor))
    block = context.render_context_block({"repo_root": str(tmp_path), "results": []})
    assert "new" in block
    rendered = context.render_context_block(
        {
            "repo_root": str(tmp_path),
            "mode": "m",
            "host": "h",
            "session_id": "s",
            "query": "q",
            "count": 1,
            "selection": {"selected_count": 1, "skipped_count": 0},
            "retrieval_diagnostics": {"status": "ok", "fallback_used": False},
            "results": [{"id": "id", "layer": "project", "score": 0.5, "recency": "now", "content": "<body>"}],
        }
    )
    assert "&lt;body&gt;" in rendered
    cursor.write_text("2021-01-01T00:00:00Z\n", encoding="utf-8")
    rendered_with_promotions = context.render_context_block(
        {
            "repo_root": str(tmp_path),
            "count": 1,
            "results": [{"id": "id", "content": "body"}],
        }
    )
    assert "mnemos-promotion" in rendered_with_promotions

    original_write_text = Path.write_text

    def fail_cursor_write(path: Path, *args: Any, **kwargs: Any) -> int:
        if path == cursor:
            raise OSError("cannot write cursor")
        return original_write_text(path, *args, **kwargs)

    cursor.write_text("2021-01-01T00:00:00Z\n", encoding="utf-8")
    monkeypatch.setattr(Path, "write_text", fail_cursor_write)
    assert "new" in context.render_promotion_block(repo_root=str(tmp_path))

    original_read_text = Path.read_text

    def fail_log_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == promo_log:
            raise OSError("cannot read log")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_log_read)
    assert context.render_promotion_block(repo_root=str(tmp_path)) == ""
    assert context._format_error(RuntimeError()) == "RuntimeError"


def test_search_middleware_backend_and_fallback_edges(tmp_path: Path) -> None:
    from core.search import SearchMiddleware, _format_error, _grep_health, _vector_trace

    class FailingFTS:
        def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
            raise RuntimeError("fts down")

    class ResultFTS:
        def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
            return [{"item_id": "fts", "content": "query content", "metadata": {"layer": "project"}}]

    class Vector:
        backend_name = "fake"
        is_available = True

        def __init__(self, *, fail: bool = False) -> None:
            self.fail = fail

        def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
            if self.fail:
                raise RuntimeError("vector down")
            return [
                {"item_id": "vector", "content": "query vector", "metadata": {"layer": "project"}},
                {"item_id": "fts", "content": "duplicate", "metadata": {"layer": "project"}},
            ]

    (tmp_path / "wiki" / "projects").mkdir(parents=True)
    (tmp_path / "wiki" / "projects" / "grep.md").write_text("query grep", encoding="utf-8")

    search = SearchMiddleware(str(tmp_path), fts_index=FailingFTS(), vector_backend=Vector(fail=True))
    results = search.search("query", limit=1)
    assert results[0]["item_id"] == "grep"
    assert search.last_diagnostics["partial_failure"] is True

    search = SearchMiddleware(str(tmp_path), fts_index=ResultFTS(), vector_backend=Vector())
    results = search.search("query", layers=["project"], limit=5)
    assert {item["item_id"] for item in results} == {"fts", "vector"}
    assert any(trace["name"] == "grep" and trace["status"] == "skipped" for trace in search.last_diagnostics["backends"])

    class Store:
        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            return [
                {"id": "store", "content": "query store", "_path": str(tmp_path / "missing.md")},
                {"id": "store", "content": "query duplicate", "_path": ""},
            ]

    search = SearchMiddleware(str(tmp_path), fts_index=FailingFTS(), vector_backend=Vector(fail=True), store=Store())
    assert search._grep_fallback("query", layers=["project"], limit=1)[0]["item_id"] == "store"
    assert search._grep_fallback("query", layers=["project"], limit=5) == [
        {"item_id": "store", "content": "query store", "metadata": {}, "score": None, "source": "grep"}
    ]

    class BadStore:
        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            raise RuntimeError("store down")

    search = SearchMiddleware(str(tmp_path), fts_index=FailingFTS(), vector_backend=Vector(fail=True), store=BadStore())
    assert search.search("query") == []
    assert search.backend_health()["status"] == "degraded"
    failed_health = SearchMiddleware(str(tmp_path / "no-search-dirs"), fts_index=FailingFTS(), vector_backend=object()).backend_health()
    assert failed_health["status"] == "failed"
    assert _vector_trace(object(), 0)["status"] == "disabled"
    assert _grep_health(tmp_path, None)["status"] == "available"
    assert _grep_health(tmp_path / "empty", None)["status"] == "unavailable"
    assert _format_error(RuntimeError()) == "RuntimeError"

    error_file = tmp_path / "wiki" / "projects" / "error.md"
    error_file.write_text("query error", encoding="utf-8")
    original_read_text = Path.read_text

    def flaky_read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == error_file:
            raise OSError("cannot read")
        return original_read_text(path, *args, **kwargs)

    import core.search as search_mod

    search = SearchMiddleware(str(tmp_path), fts_index=FailingFTS(), vector_backend=Vector(fail=True))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "read_text", flaky_read)
        assert search._grep_fallback("query", layers=["project"], limit=5)
    assert search._get_search_dirs(["project", "missing"])
    (tmp_path / "wiki" / "global").mkdir(parents=True, exist_ok=True)
    (tmp_path / "wiki" / "projects" / "dup.md").write_text("query duplicate project", encoding="utf-8")
    (tmp_path / "wiki" / "global" / "dup.md").write_text("query duplicate global", encoding="utf-8")
    legacy = SearchMiddleware(str(tmp_path), fts_index=FailingFTS(), vector_backend=Vector(fail=True))
    assert [item["item_id"] for item in legacy._grep_fallback("query", layers=["project", "global"], limit=5)].count("dup") == 1


def test_transcript_loading_extraction_and_capture_edges(tmp_path: Path) -> None:
    from core.transcript import (
        TranscriptInsight,
        _clean_lines,
        _is_trivial,
        _layer_for_content,
        _looks_internal,
        _message_text,
        capture_transcript,
        extract_insights,
        load_transcript,
    )

    assert TranscriptInsight("content", "project", "kind", 3).source_key == "kind:3"
    assert _message_text({"content": "plain"}) == "plain"
    assert _message_text({"content": [{"type": "text", "text": "a"}, {"type": "image", "text": "b"}]}) == "a"
    assert _message_text({"content": 1}) == ""

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert load_transcript(empty) == []
    list_file = tmp_path / "list.json"
    list_file.write_text(json.dumps([{"role": "assistant"}, "bad"]), encoding="utf-8")
    assert load_transcript(list_file) == [{"role": "assistant"}]
    events_file = tmp_path / "events.json"
    events_file.write_text(json.dumps({"events": [{"role": "assistant"}]}), encoding="utf-8")
    assert load_transcript(events_file) == [{"role": "assistant"}]
    single_file = tmp_path / "single.json"
    single_file.write_text(json.dumps({"role": "assistant"}), encoding="utf-8")
    assert load_transcript(single_file) == [{"role": "assistant"}]
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_text('{"role":"assistant"}\nnot-json\n[]\n', encoding="utf-8")
    assert load_transcript(jsonl) == [{"role": "assistant"}]

    lines, dropped = _clean_lines("```code```\nSTATUS: hidden\n|---|\n-----\n- /tmp/path\nDecision: project architecture is fixed because workflow continuity matters.")
    assert dropped >= 3
    assert lines
    assert _looks_internal("TASK_ID: 1 handoff", [], 1) is True
    assert _is_trivial("ok") is True
    long_lines = [f"line {i} implemented verification workflow continuity rationale" for i in range(10)]
    assert len(" ".join(long_lines)) > len(" ".join(long_lines[:5]))
    assert _layer_for_content("global convention preference: use this everywhere") == "global"

    messages = [
        {"role": "user", "content": "ignore"},
        {"role": "assistant", "content": "✻ 🧠 global preference captured"},
        {"role": "assistant", "content": "Decision: project architecture is fixed because workflow continuity matters."},
        {"role": "assistant", "content": "## Header\nSummary: implemented workflow memory continuity with verification and rationale."},
        {"role": "assistant", "content": "Implemented memory workflow continuity verification and rationale across long-running architecture decisions with changed files and tests."},
        {"role": "assistant", "content": "TASK_ID: 1\nhandoff pipeline status"},
    ]
    insights = extract_insights(messages)
    assert {insight.kind for insight in insights} >= {"marker", "durable-line", "paragraph"}

    class FakeGateway:
        def __init__(self) -> None:
            self.last_capture_was_duplicate = False
            self.calls = 0

        def capture(self, **kwargs: Any) -> str | None:
            self.calls += 1
            if self.calls == 1:
                self.last_capture_was_duplicate = True
                return None
            if self.calls == 2:
                raise RuntimeError("capture failed")
            self.last_capture_was_duplicate = False
            return f"id-{self.calls}"

    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps(messages), encoding="utf-8")
    payload = capture_transcript(transcript_path=transcript, session_id="s", host="test", gateway=FakeGateway())
    assert payload["duplicate_count"] == 1
    assert payload["skipped_count"] == 1


def test_daemon_management_success_and_error_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.daemon as daemon

    plist = tmp_path / "gc.plist"
    daemon_plist = tmp_path / "daemon.plist"
    monkeypatch.setattr(daemon, "PLIST_PATH", plist)
    monkeypatch.setattr(daemon, "DAEMON_PLIST_PATH", daemon_plist)
    monkeypatch.setattr(daemon, "LAUNCH_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(daemon, "GC_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(daemon, "GC_LOG_PATH", tmp_path / "logs" / "gc.log")
    monkeypatch.setattr(daemon, "GC_ERROR_LOG_PATH", tmp_path / "logs" / "gc-error.log")
    monkeypatch.setattr(daemon, "DAEMON_LOG_PATH", tmp_path / "logs" / "daemon.log")
    monkeypatch.setattr(daemon, "DAEMON_ERROR_LOG_PATH", tmp_path / "logs" / "daemon-error.log")
    monkeypatch.setattr(daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(daemon.shutil, "which", lambda name: "/bin/mnemos")

    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    daemon.install_gc_daemon()
    assert plist.exists()
    daemon.uninstall_gc_daemon()
    assert not plist.exists()

    plist.write_text("plist", encoding="utf-8")

    def unload_warning(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "unload" in args:
            raise subprocess.CalledProcessError(7, args, stderr="not loaded")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(daemon.subprocess, "run", unload_warning)
    daemon.uninstall_gc_daemon()

    monkeypatch.setattr(daemon.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        daemon.install_gc_daemon()
    with pytest.raises(FileNotFoundError):
        daemon.install_autonomous_daemon()

    monkeypatch.setattr(daemon.shutil, "which", lambda name: "/bin/mnemos")
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    daemon.install_autonomous_daemon()
    assert daemon_plist.exists()
    monkeypatch.setattr(
        daemon.subprocess,
        "run",
        lambda args, **kwargs: (_ for _ in ()).throw(subprocess.CalledProcessError(8, args, stderr="not loaded")),
    )
    daemon.uninstall_autonomous_daemon()
    daemon_plist.write_text("plist", encoding="utf-8")
    monkeypatch.setattr(daemon.subprocess, "run", fake_run)
    daemon.uninstall_autonomous_daemon()
    assert not daemon_plist.exists()

    with pytest.raises(FileNotFoundError):
        daemon.uninstall_autonomous_daemon()

    class BgResult:
        ran = False
        messages: list[str] = []
        gc_archived = 0
        promoted = 0

    class FakeGateway:
        _root = tmp_path

    import core.bg as bg_mod
    import core.gateway as gateway_mod

    monkeypatch.setattr(bg_mod, "run_background_check", lambda **kwargs: BgResult())
    monkeypatch.setattr(gateway_mod, "MemoryGateway", lambda: FakeGateway())
    assert daemon.run_autonomous_daemon()["status"] == "skipped"

    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("launchctl missing")))
    assert daemon.status_autonomous_daemon()["loaded"] is None

    monkeypatch.setattr(daemon.platform, "system", lambda: "Linux")
    with pytest.raises(RuntimeError):
        daemon._require_macos()
    assert daemon.manage_gc_daemon(install=False, uninstall=False) is None
    with pytest.raises(SystemExit):
        daemon.manage_gc_daemon(install=True, uninstall=True)
    with pytest.raises(ValueError):
        daemon.manage_autonomous_daemon("unknown")

    monkeypatch.setattr(daemon, "install_gc_daemon", lambda: (_ for _ in ()).throw(FileNotFoundError("missing mnemos")))
    with pytest.raises(SystemExit):
        daemon.manage_gc_daemon(install=True, uninstall=False)
    monkeypatch.setattr(
        daemon,
        "install_gc_daemon",
        lambda: (_ for _ in ()).throw(subprocess.CalledProcessError(9, ["launchctl"], stderr="boom")),
    )
    with pytest.raises(SystemExit):
        daemon.manage_gc_daemon(install=True, uninstall=False)

    status_payload = {
        "label": "dev.mnemos.daemon",
        "installed": False,
        "platform": "Darwin",
        "supported": True,
        "plist_path": str(daemon_plist),
        "loaded": False,
    }
    monkeypatch.setattr(daemon.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(daemon, "status_autonomous_daemon", lambda: dict(status_payload))
    monkeypatch.setattr(daemon, "install_autonomous_daemon", lambda: None)
    monkeypatch.setattr(daemon, "uninstall_autonomous_daemon", lambda: None)
    daemon.manage_autonomous_daemon("status")
    daemon.manage_autonomous_daemon("install")
    daemon.manage_autonomous_daemon("uninstall")
    daemon.manage_autonomous_daemon("status", as_json=True)

    monkeypatch.setattr(daemon, "install_autonomous_daemon", lambda: (_ for _ in ()).throw(FileNotFoundError("missing daemon")))
    with pytest.raises(SystemExit):
        daemon.manage_autonomous_daemon("install", as_json=True)
    with pytest.raises(SystemExit):
        daemon.manage_autonomous_daemon("install", as_json=False)

    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no launchctl")))
    daemon._launchctl_unload_if_loaded(daemon_plist)
