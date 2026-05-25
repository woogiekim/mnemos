"""Regression tests for deterministic autonomous memory V1."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from core.cli import cli


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
    assert not any("TASK_ID:" in content for content in contents)
    assert len(contents) == len(set(contents))


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
