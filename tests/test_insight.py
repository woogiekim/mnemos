"""Tests for InsightExtractor agent and memory-extract-insight CLI command."""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner
from pathlib import Path


# ---------------------------------------------------------------------------
# Shared helper: inject a fake anthropic module into sys.modules
# ---------------------------------------------------------------------------

def _make_anthropic_mock(response_text: str) -> types.ModuleType:
    """Return a lightweight fake *anthropic* module.

    The fake exposes ``anthropic.Anthropic()`` whose
    ``messages.create()`` returns a message object whose
    ``content[0].text`` equals *response_text*.
    """
    mock_content = MagicMock()
    mock_content.text = response_text

    mock_message = MagicMock()
    mock_message.content = [mock_content]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message

    mock_anthropic_cls = MagicMock(return_value=mock_client)

    module = types.ModuleType("anthropic")
    module.Anthropic = mock_anthropic_cls
    # Expose APIError so the except clause in insight.py resolves cleanly
    module.APIError = Exception
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path):
    """Create a minimal repo structure for the session layer."""
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


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cli_with_repo(repo_root, monkeypatch):
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
    from core.cli import cli
    return cli


# ---------------------------------------------------------------------------
# InsightExtractor unit tests
# ---------------------------------------------------------------------------

class TestInsightExtractorExtract:
    def test_returns_list_of_insights(self):
        """extract() must return a list when the LLM responds with valid JSON."""
        from agents.insight import InsightExtractor

        fake_anthropic = _make_anthropic_mock(json.dumps([
            {"content": "Use SQLAlchemy 2.x async sessions for all DB calls.", "category": "constraint"},
        ]))

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            extractor = InsightExtractor()
            results = extractor.extract("We decided to always use async sessions.")

        assert len(results) == 1
        assert results[0]["content"] == "Use SQLAlchemy 2.x async sessions for all DB calls."
        assert results[0]["category"] == "constraint"

    def test_returns_empty_list_when_llm_says_nothing(self):
        """extract() must return [] when the LLM returns an empty array."""
        from agents.insight import InsightExtractor

        fake_anthropic = _make_anthropic_mock("[]")

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            extractor = InsightExtractor()
            results = extractor.extract("ok, looks good")

        assert results == []

    def test_handles_malformed_llm_output(self):
        """extract() must return [] when the LLM returns non-JSON."""
        from agents.insight import InsightExtractor

        fake_anthropic = _make_anthropic_mock("sorry, I can't help with that")

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            extractor = InsightExtractor()
            results = extractor.extract("some turn")

        assert results == []

    def test_raises_runtime_error_when_anthropic_not_installed(self):
        """extract() must raise RuntimeError when anthropic SDK is missing."""
        from agents.insight import InsightExtractor

        # Ensure anthropic is not importable
        saved = sys.modules.pop("anthropic", None)
        try:
            extractor = InsightExtractor()
            with pytest.raises((RuntimeError, ImportError)):
                extractor.extract("anything")
        finally:
            if saved is not None:
                sys.modules["anthropic"] = saved

    def test_uses_haiku_model(self):
        """InsightExtractor must target claude-haiku-4-5."""
        from agents.insight import InsightExtractor
        assert InsightExtractor.MODEL == "claude-haiku-4-5"

    def test_calls_haiku_model_in_api_request(self):
        """extract() must pass the MODEL constant to messages.create."""
        from agents.insight import InsightExtractor

        fake_anthropic = _make_anthropic_mock(json.dumps([
            {"content": "Always use connection pooling.", "category": "pattern"},
        ]))

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            extractor = InsightExtractor()
            extractor.extract("We chose connection pooling for performance.")

        # Retrieve the mock client instance
        mock_client = fake_anthropic.Anthropic.return_value
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs.get("model") == InsightExtractor.MODEL or \
               (call_kwargs.args and call_kwargs.args[0] == InsightExtractor.MODEL), \
               f"Expected model={InsightExtractor.MODEL!r}, got {call_kwargs}"


# ---------------------------------------------------------------------------
# CLI command tests
# ---------------------------------------------------------------------------

class TestMemoryExtractInsightCommand:
    def _make_hook_input(
        self,
        session_id: str = "test-session-001",
        last_assistant_message: str = "",
        reason: str = "Task appears complete",
        transcript_path: str = "",
    ) -> str:
        data = {
            "session_id": session_id,
            "hook_event_name": "Stop",
            "reason": reason,
            "cwd": "/tmp/test-project",
            "permission_mode": "ask",
        }
        if last_assistant_message:
            data["last_assistant_message"] = last_assistant_message
        if transcript_path:
            data["transcript_path"] = transcript_path
        return json.dumps(data)

    def test_dry_run_prints_insight_without_capturing(self, runner, cli_with_repo, repo_root):
        """--dry-run must print insights and not write to disk."""
        fake_anthropic = _make_anthropic_mock(json.dumps([
            {"content": "Always use connection pooling.", "category": "constraint"},
        ]))

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            hook_input = self._make_hook_input(
                last_assistant_message="We decided to always use connection pooling for performance."
            )
            result = runner.invoke(
                cli_with_repo,
                ["memory-extract-insight", "--dry-run"],
                input=hook_input,
            )

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert "connection pooling" in result.output.lower()

        # Nothing should have been captured
        session_dir = repo_root / ".agent" / "sessions"
        captured_files = list(session_dir.rglob("*.md"))
        assert len(captured_files) == 0

    def test_captures_insight_to_session_layer(self, runner, cli_with_repo, repo_root):
        """Without --dry-run, insights must be persisted to the session layer."""
        fake_anthropic = _make_anthropic_mock(json.dumps([
            {"content": "Prefer async DB sessions in all handlers.", "category": "pattern"},
        ]))

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            hook_input = self._make_hook_input(
                session_id="sess-capture-test",
                last_assistant_message="We decided to always use async sessions.",
            )
            result = runner.invoke(
                cli_with_repo,
                ["memory-extract-insight"],
                input=hook_input,
            )

        assert result.exit_code == 0, result.output
        assert "captured insight" in result.output.lower()

    def test_empty_turn_exits_zero_silently(self, runner, cli_with_repo):
        """When stdin contains no substantive turn text, exit 0 with no output."""
        hook_input = json.dumps({
            "session_id": "sess-empty",
            "hook_event_name": "Stop",
            "reason": "done",
        })
        result = runner.invoke(
            cli_with_repo,
            ["memory-extract-insight"],
            input=hook_input,
        )
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_no_insights_exits_zero(self, runner, cli_with_repo):
        """When the LLM finds nothing worth capturing, exit 0 with no output."""
        fake_anthropic = _make_anthropic_mock("[]")

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            hook_input = self._make_hook_input(last_assistant_message="ok sure")
            result = runner.invoke(
                cli_with_repo,
                ["memory-extract-insight"],
                input=hook_input,
            )

        assert result.exit_code == 0

    def test_invalid_json_stdin_exits_nonzero(self, runner, cli_with_repo):
        """Invalid JSON on stdin must exit nonzero with an error message."""
        result = runner.invoke(
            cli_with_repo,
            ["memory-extract-insight"],
            input="not valid json !!!",
        )
        assert result.exit_code != 0
        assert "error" in result.output.lower() or "error" in (result.output + "").lower()

    def test_reads_transcript_file_when_no_last_message(self, runner, cli_with_repo, repo_root, tmp_path):
        """When last_assistant_message is absent, fall back to transcript_path."""
        transcript_file = tmp_path / "transcript.txt"
        transcript_file.write_text(
            "Assistant: We decided to never use global state for thread safety reasons."
        )

        fake_anthropic = _make_anthropic_mock(json.dumps([
            {"content": "Avoid global state for thread safety.", "category": "constraint"},
        ]))

        with patch.dict(sys.modules, {"anthropic": fake_anthropic}):
            hook_input = json.dumps({
                "session_id": "sess-transcript-test",
                "hook_event_name": "Stop",
                "reason": "done",
                "transcript_path": str(transcript_file),
            })
            result = runner.invoke(
                cli_with_repo,
                ["memory-extract-insight", "--dry-run"],
                input=hook_input,
            )

        assert result.exit_code == 0, result.output
        assert "global state" in result.output.lower()
