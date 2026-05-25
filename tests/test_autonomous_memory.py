"""Regression tests for deterministic autonomous memory V1."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli
from core.context import render_context_block, retrieve_context
from core.gateway import MemoryGateway


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a minimal mnemos repo for autonomous memory tests."""
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for dirname in ["runs", "sessions", "state", "reports", "tools", "transient"]:
        (agent / dirname).mkdir(parents=True)

    policy = {
        "layers": {
            "transient": {"path_template": ".agent/transient/", "promotes_to": None, "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 1.0}},
            "ephemeral": {"path_template": ".agent/runs/{run_id}/scratch/", "promotes_to": "working", "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 1.0}},
            "working": {"path_template": ".agent/runs/{run_id}/working/", "promotes_to": "session", "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 1.0}},
            "session": {"path_template": ".agent/sessions/{session_id}/", "promotes_to": "project", "promotion": {"age_hours": 1.0, "access_count": 10, "quality_score": 1.0}},
            "project": {"path_template": "wiki/projects/", "promotes_to": "global", "promotion": {"age_hours": 1.0, "access_count": 10, "quality_score": 1.0}},
            "global": {"path_template": "wiki/global/", "promotes_to": None, "promotion": {"age_hours": 1.0, "access_count": 10, "quality_score": 1.0}},
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")
    return tmp_path


def test_context_json_and_render_shapes(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """context emits provider JSON and bounded render blocks."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    runner = CliRunner()

    capture = runner.invoke(cli, [
        "capture", "--json", "--layer", "project", "--id", "ctx-001",
        "--content", "Architecture decision: use deterministic context retrieval",
        "--no-classify",
    ])
    assert capture.exit_code == 0, capture.output

    result = runner.invoke(cli, [
        "context", "--json", "--prompt", "How does deterministic context work?",
        "--session-id", "sess-1", "--host", "claude-code",
    ])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provider"] == "mnemos"
    assert payload["mode"] == "deterministic-v1"
    assert payload["host"] == "claude-code"
    assert payload["results"][0]["id"] == "ctx-001"
    assert {"id", "layer", "score", "recency", "content"}.issubset(payload["results"][0])

    rendered = runner.invoke(cli, [
        "context", "--render", "--prompt", "deterministic context",
        "--session-id", "sess-1", "--host", "claude-code",
    ])
    assert rendered.exit_code == 0, rendered.output
    assert rendered.output.startswith("<mnemos-context")
    assert 'advisory="true"' in rendered.output
    assert '<memory id="ctx-001"' in rendered.output


def test_capture_transcript_filters_deduplicates_and_captures(repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """capture-transcript extracts durable insights and skips control noise."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    transcript = tmp_path / "transcript.json"
    transcript.write_text(json.dumps([
        {"role": "assistant", "content": "STATUS: completed\nTASK_ID: abc\nPLAN:\n  actions: write pipeline.json"},
        {"role": "assistant", "content": "✻ 💾 Architecture decision: deterministic V1 retrieval only\n✻ 💾 Architecture decision: deterministic V1 retrieval only"},
        {"role": "assistant", "content": "Done."},
        {"role": "assistant", "content": "Root cause: transcript capture previously depended on assistant-authored protocol reminders."},
    ]), encoding="utf-8")

    result = CliRunner().invoke(cli, [
        "capture-transcript", "--json", "--transcript-path", str(transcript),
        "--session-id", "sess-2", "--host", "claude-code",
    ])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    contents = [item["content"] for item in payload["captures"]]
    assert "Architecture decision: deterministic V1 retrieval only" in contents
    assert any("Root cause:" in content for content in contents)
    assert not any(content.startswith("AI conversation insight: Root cause:") for content in contents)
    assert not any("TASK_ID:" in content for content in contents)
    assert len(contents) == len(set(contents))


def test_context_respects_strict_budget(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Context selection keeps total injected content under the requested budget."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    gw = MemoryGateway(repo_root=str(repo_root))
    for idx in range(3):
        gw.capture(
            layer="project",
            item_id=f"budget-{idx}",
            content=(f"memory budget injection item {idx} " + ("detail " * 40)).strip(),
            quality_score=0.9,
            no_classify=True,
        )

    payload = retrieve_context(
        prompt="memory budget injection",
        session_id="sess-budget",
        host="test",
        gateway=gw,
        limit=3,
        max_chars=120,
    )

    assert payload["used_chars"] <= 120
    assert sum(len(item["content"]) for item in payload["results"]) <= 120
    assert payload["selection"]["skipped_count"] >= 1


def test_context_suppresses_stale_and_noisy_memories(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Old low-confidence memories and protocol noise are skipped quietly."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    gw = MemoryGateway(repo_root=str(repo_root))
    old = (datetime.now(timezone.utc) - timedelta(days=900)).isoformat() + "Z"
    gw.capture(
        layer="project",
        item_id="ctx-stale",
        content="deterministic retrieval context should always inject this old idea",
        quality_score=0.1,
        extra_metadata={"created_at": old, "confidence": 0.1},
        no_classify=True,
    )
    gw.capture(
        layer="project",
        item_id="ctx-noisy",
        content="STATUS: completed\nTASK_ID: abc\nPLAN:\n  actions: inject deterministic retrieval context",
        quality_score=0.9,
        no_classify=True,
    )
    gw.capture(
        layer="project",
        item_id="ctx-good",
        content="Architecture decision: deterministic retrieval context stays advisory and bounded",
        quality_score=0.95,
        no_classify=True,
    )

    payload = retrieve_context(
        prompt="deterministic retrieval context",
        session_id="sess-filter",
        host="test",
        gateway=gw,
        limit=5,
    )

    ids = [item["id"] for item in payload["results"]]
    assert "ctx-good" in ids
    assert "ctx-stale" not in ids
    assert "ctx-noisy" not in ids
    assert payload["selection"]["skipped_reasons"]["stale"] >= 1
    assert payload["selection"]["skipped_reasons"]["noisy"] >= 1


def test_context_prioritizes_high_signal_memory(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dense, fresh, confident memory outranks broader lower-signal matches."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    gw = MemoryGateway(repo_root=str(repo_root))
    gw.capture(
        layer="project",
        item_id="ctx-broad",
        content="retrieval scoring relevance freshness confidence signal density " + ("background note " * 80),
        quality_score=0.4,
        no_classify=True,
    )
    gw.capture(
        layer="project",
        item_id="ctx-dense",
        content="retrieval scoring ranks relevance freshness confidence signal density",
        quality_score=0.95,
        extra_metadata={"confidence": 0.95},
        no_classify=True,
    )

    payload = retrieve_context(
        prompt="retrieval scoring relevance freshness confidence signal density",
        session_id="sess-signal",
        host="test",
        gateway=gw,
        limit=2,
    )

    assert payload["results"][0]["id"] == "ctx-dense"
    assert payload["results"][0]["score_components"]["signal_density"] > payload["results"][1]["score_components"]["signal_density"]


def test_context_quietly_degrades_for_empty_and_failed_retrieval(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty or failed recall produces no rendered injection block."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    runner = CliRunner()

    empty = runner.invoke(cli, ["context", "--render", "--prompt", "missing memory xyz"])
    assert empty.exit_code == 0, empty.output
    assert empty.output == ""

    class BrokenGateway:
        _root = repo_root

        def search(self, query: str, limit: int) -> list[dict[str, object]]:
            raise RuntimeError("search unavailable")

    payload = retrieve_context(
        prompt="anything",
        session_id="sess-broken",
        host="test",
        gateway=BrokenGateway(),  # type: ignore[arg-type]
    )

    assert payload["status"] == "degraded"
    assert payload["results"] == []
    assert render_context_block(payload) == ""


def test_capabilities_include_autonomous_flags() -> None:
    """Provider capabilities report autonomous flags and host degradation."""
    result = CliRunner().invoke(cli, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["capability_status"]["autonomous_capture"] == "supported"
    assert payload["capability_status"]["context_injection"] == "supported"
    assert payload["capability_status"]["daemon_runtime"] == "supported"
    assert payload["host_capability_status"]["cursor"]["autonomous_capture"] == "unsupported"
    assert payload["host_capability_status"]["codex"]["context_injection"] == "unknown"


def test_daemon_status_json_shape() -> None:
    """daemon status works without invoking launchctl install operations."""
    result = CliRunner().invoke(cli, ["daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["label"] == "com.mnemos.daemon"
    assert "installed" in payload
    assert "supported" in payload


def test_daemon_run_json_passes_repo_root(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon run resolves the configured repo root for background checks."""
    from core.bg import BackgroundCheckResult

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    calls: dict[str, object] = {}

    def fake_run_background_check(repo_root: str, **kwargs: object) -> BackgroundCheckResult:
        calls["repo_root"] = repo_root
        calls["kwargs"] = kwargs
        return BackgroundCheckResult(ran=True, gc_archived=1, promoted=2)

    monkeypatch.setattr("core.bg.run_background_check", fake_run_background_check)

    result = CliRunner().invoke(cli, ["daemon", "run", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert calls["repo_root"] == str(repo_root)
    assert calls["kwargs"] == {"interval_minutes": 0}
    assert payload["status"] == "completed"
    assert payload["gc_archived"] == 1
    assert payload["promoted"] == 2
