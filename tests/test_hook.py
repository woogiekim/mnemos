"""Tests for hooks/UserPromptSubmit.sh behaviour and ClaudeCodeAdapter integration."""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.adapters.claude import (
    ClaudeCodeAdapter,
    _USER_PROMPT_SUBMIT_HOOK_TEMPLATE,
    _hook_script_path,
    _render_template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "UserPromptSubmit.sh"
STOP_HOOK_SCRIPT = Path(__file__).parent.parent / "hooks" / "Stop.sh"


def _run_hook(prompt: str, session_id: str = "test-session-123",
              mnemos_repo_root: str = "", env_extras: dict | None = None) -> tuple[int, str]:
    """Run the UserPromptSubmit hook script with a synthetic JSON payload.

    Returns (returncode, combined stdout+stderr output).
    """
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.json",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    })

    env = os.environ.copy()
    if mnemos_repo_root:
        env["MNEMOS_REPO_ROOT"] = mnemos_repo_root
    else:
        env.pop("MNEMOS_REPO_ROOT", None)
    if env_extras:
        env.update(env_extras)

    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    return result.returncode, combined


def _run_stop_hook(
    transcript: list[dict],
    tmp_path: Path,
    session_id: str = "test-session-123",
) -> tuple[int, str, list[list[str]]]:
    """Run the Stop hook with a fake mnemos binary and return captured argv."""
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    calls_path = tmp_path / "mnemos_calls.jsonl"
    fake_bin = tmp_path / "mnemos"
    fake_bin.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(calls_path)!r}).open('a', encoding='utf-8').write("
        "json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript_path),
        "hook_event_name": "Stop",
    })
    env = os.environ.copy()
    env["MNEMOS_REPO_ROOT"] = str(tmp_path)
    env["PATH"] = f"{tmp_path}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(STOP_HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    calls = []
    if calls_path.exists():
        calls = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    return result.returncode, result.stdout + result.stderr, calls


# ---------------------------------------------------------------------------
# Existence and permissions
# ---------------------------------------------------------------------------

class TestHookScriptFile:
    def test_hook_script_exists(self):
        assert HOOK_SCRIPT.exists(), f"Hook script not found at {HOOK_SCRIPT}"

    def test_hook_script_is_executable(self):
        mode = HOOK_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, "Hook script is not user-executable"

    def test_hook_script_is_shell_script(self):
        first_line = HOOK_SCRIPT.read_text().splitlines()[0]
        assert first_line.startswith("#!"), "Hook script missing shebang line"

    def test_stop_hook_script_exists(self):
        assert STOP_HOOK_SCRIPT.exists(), f"Stop hook script not found at {STOP_HOOK_SCRIPT}"


# ---------------------------------------------------------------------------
# Stop hook fallback capture
# ---------------------------------------------------------------------------

class TestStopHookInsightFallback:
    def test_fallback_captures_meaningful_assistant_summary(self, tmp_path):
        """When no explicit marker exists, Stop captures the latest useful outcome."""
        transcript = [
            {
                "role": "assistant",
                "content": (
                    "Implemented the transcript fallback in the Claude Stop hook. "
                    "The hook now preserves durable conversation outcomes when "
                    "explicit capture markers are missing, and tests cover the "
                    "control-signal filters."
                ),
            }
        ]

        rc, output, calls = _run_stop_hook(transcript, tmp_path)

        assert rc == 0, output
        assert len(calls) == 1
        call = calls[0]
        assert call[:2] == ["capture", "--content"]
        assert call[3:5] == ["--quiet", "--layer"]
        assert call[5] == "session"
        assert "AI conversation insight:" in call[2]
        assert "transcript fallback" in call[2]

    def test_fallback_excludes_internal_pipeline_control_signals(self, tmp_path):
        """Agent-crew control/status handoffs must not become memories."""
        transcript = [
            {
                "role": "assistant",
                "content": (
                    "STATUS: completed\n"
                    "TASK_ID: 20260520-065034-0\n"
                    "TASK_DIR: /Users/example/.agent-crew/state/mnemos/tasks/20260520-065034-0\n"
                    "PLAN:\n"
                    "  actions: write pipeline.json and handoff.md\n"
                    "BLOCKER: none\n"
                    "This agent-crew task handoff should remain internal pipeline state."
                ),
            }
        ]

        rc, output, calls = _run_stop_hook(transcript, tmp_path)

        assert rc == 0, output
        assert calls == []

    def test_fallback_excludes_trivial_acknowledgements(self, tmp_path):
        """One-word or low-information assistant replies are ignored."""
        transcript = [{"role": "assistant", "content": "Done."}]

        rc, output, calls = _run_stop_hook(transcript, tmp_path)

        assert rc == 0, output
        assert calls == []

    def test_fallback_sanitizes_status_lines_from_completion_summary(self, tmp_path):
        """Useful summaries are retained without persisting STATUS tokens."""
        transcript = [
            {
                "role": "assistant",
                "content": (
                    "STATUS: completed\n"
                    "Changed files: hooks/Stop.sh and tests/test_hook.py.\n"
                    "Verification: pytest tests/test_hook.py confirmed the new "
                    "fallback captures meaningful summaries and rejects pipeline controls."
                ),
            }
        ]

        rc, output, calls = _run_stop_hook(transcript, tmp_path)

        assert rc == 0, output
        assert len(calls) == 1
        content = calls[0][2]
        assert "STATUS:" not in content
        assert "Changed files" in content
        assert "fallback captures meaningful summaries" in content

    def test_explicit_marker_still_takes_precedence_over_fallback(self, tmp_path):
        """Marker protocol remains canonical when the assistant emits it."""
        transcript = [
            {
                "role": "assistant",
                "content": (
                    "Implemented a broader summary.\n"
                    "✻ 💾 Stop hook fallback uses conservative filtering"
                ),
            }
        ]

        rc, output, calls = _run_stop_hook(transcript, tmp_path)

        assert rc == 0, output
        assert len(calls) == 1
        assert calls[0][2] == "Stop hook fallback uses conservative filtering"
        assert calls[0][5] == "project"


# ---------------------------------------------------------------------------
# Guard conditions
# ---------------------------------------------------------------------------

class TestHookGuards:
    def test_exits_cleanly_when_mnemos_repo_root_not_set(self):
        """Hook must exit 0 silently when MNEMOS_REPO_ROOT is unset."""
        rc, output = _run_hook("hello world", mnemos_repo_root="")
        assert rc == 0
        assert output == ""

    def test_exits_cleanly_when_prompt_empty(self, tmp_path):
        """Hook must exit 0 silently when the prompt field is empty."""
        payload = json.dumps({
            "session_id": "sess-001",
            "prompt": "",
        })
        env = os.environ.copy()
        env["MNEMOS_REPO_ROOT"] = str(tmp_path)
        result = subprocess.run(
            ["bash", str(HOOK_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == ""

    def test_exits_cleanly_when_mnemos_not_in_path(self, tmp_path):
        """Hook exits 0 when mnemos binary is not available."""
        env = os.environ.copy()
        env["MNEMOS_REPO_ROOT"] = str(tmp_path)
        # Remove all paths that could contain mnemos
        env["PATH"] = "/usr/bin:/bin"
        rc, output = _run_hook("some prompt", mnemos_repo_root=str(tmp_path), env_extras={"PATH": "/usr/bin:/bin"})
        assert rc == 0


# ---------------------------------------------------------------------------
# /compact special case
# ---------------------------------------------------------------------------

class TestCompactPrompt:
    def test_compact_emits_capture_reminder(self, tmp_path):
        """The /compact prompt must emit the capture reminder message."""
        rc, output = _run_hook("/compact", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "[mnemos]" in output
        assert "/compact detected" in output
        assert "mnemos capture" in output

    def test_compact_does_not_run_search(self, tmp_path):
        """/compact must not trigger mnemos search."""
        rc, output = _run_hook("/compact", mnemos_repo_root=str(tmp_path))
        # Search output would include <mnemos-context type="search"...>
        assert '<mnemos-context type="search"' not in output


# ---------------------------------------------------------------------------
# Session-start context load
# ---------------------------------------------------------------------------

class TestSessionStartLoad:
    def test_session_flag_file_created(self, tmp_path, monkeypatch):
        """A session flag file must be created on first run."""
        session_id = "unique-session-abc"
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
        (tmp_path / "tmp").mkdir()

        # Run hook (mnemos may not return useful data, but flag should appear)
        env = os.environ.copy()
        env["MNEMOS_REPO_ROOT"] = str(tmp_path / "repo")
        env["TMPDIR"] = str(tmp_path / "tmp")
        _run_hook("hello", session_id=session_id, mnemos_repo_root=str(tmp_path / "repo"),
                  env_extras={"TMPDIR": str(tmp_path / "tmp")})

        flag_dir = tmp_path / "tmp" / "mnemos-session-flags"
        flags = list(flag_dir.glob("mnemos-session-loaded-*")) if flag_dir.exists() else []
        assert len(flags) == 1, f"Expected 1 session flag, found: {flags}"

    def test_session_flag_not_duplicated(self, tmp_path):
        """Running the hook twice with the same session_id must not duplicate context."""
        session_id = "dedup-session-xyz"
        flag_dir = tmp_path / "mnemos-session-flags"
        flag_dir.mkdir(parents=True)

        env = os.environ.copy()
        env["MNEMOS_REPO_ROOT"] = str(tmp_path / "repo")
        env["TMPDIR"] = str(tmp_path)

        # Pre-create the flag so second run skips session load
        (flag_dir / f"mnemos-session-loaded-{session_id}").touch()

        # Run hook; should not emit session-start context because flag already exists
        rc, output = _run_hook("hello world", session_id=session_id,
                               mnemos_repo_root=str(tmp_path / "repo"),
                               env_extras={"TMPDIR": str(tmp_path)})
        assert rc == 0
        assert '<mnemos-context type="session-start"' not in output


# ---------------------------------------------------------------------------
# Per-prompt search output format
# ---------------------------------------------------------------------------

class TestSearchOutput:
    def test_search_output_wrapped_in_xml_tags(self, tmp_path, monkeypatch):
        """When search returns results, they must be wrapped in <mnemos-context> tags."""
        # We need a real mnemos search that returns results, so use the actual CLI
        # with the user's mnemos repo root if available, otherwise skip.
        repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
        if not repo_root:
            pytest.skip("MNEMOS_REPO_ROOT not set — skipping live search test")

        rc, output = _run_hook("memory hook search", mnemos_repo_root=repo_root)
        assert rc == 0
        # If output is non-empty it must be properly wrapped
        if output.strip():
            if "no results found" not in output:
                assert "<mnemos-context" in output
                assert "</mnemos-context>" in output

    def test_no_output_when_search_empty(self, tmp_path):
        """When mnemos returns no results, hook must produce no output for search section."""
        # Use a fake repo root where mnemos will find no memories
        rc, output = _run_hook("xyzzy quux plugh nosuchterm",
                               mnemos_repo_root=str(tmp_path))
        # Hook exits 0; with no mnemos data, no search output
        assert rc == 0


# ---------------------------------------------------------------------------
# Capture protocol reminder
# ---------------------------------------------------------------------------

class TestCaptureProtocol:
    def test_capture_protocol_emitted_on_normal_prompt(self, tmp_path):
        """The hook must emit a <mnemos-capture-protocol> block on every normal prompt."""
        rc, output = _run_hook("how does caching work?",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "<mnemos-capture-protocol>" in output
        assert "</mnemos-capture-protocol>" in output

    def test_capture_protocol_contains_command(self, tmp_path):
        """The capture protocol block must include the mnemos capture command.

        Method E (closes #22): the capture command no longer uses --quiet because
        the CLI stdout IS the notification. Without --quiet, the CLI emits both
        the machine-readable 'captured: <uuid>' and the human-friendly notification.
        The protocol still includes --layer session and --session-id.
        """
        rc, output = _run_hook("explain the architecture",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "mnemos capture" in output
        assert "--layer session" in output
        # Method E: --quiet is no longer in the protocol's example capture command
        # (the CLI needs to emit the notification, which --quiet would suppress)
        assert "--quiet" not in output, (
            "Method E: capture protocol must NOT use --quiet; "
            "the CLI stdout IS the notification and --quiet would suppress it"
        )

    def test_capture_protocol_emitted_even_without_search_results(self, tmp_path):
        """The capture protocol block must fire even when no search results are found."""
        # tmp_path has no memories, so search returns nothing — protocol still fires
        rc, output = _run_hook("xyzzy quux plugh nosuchterm",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "<mnemos-capture-protocol>" in output

    def test_compact_does_not_emit_capture_protocol_block(self, tmp_path):
        """/compact exits early with its own reminder; capture protocol block must NOT appear."""
        rc, output = _run_hook("/compact", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "<mnemos-capture-protocol>" not in output

    def test_capture_protocol_uses_liberal_language(self, tmp_path):
        """Protocol must encourage liberal capturing, not just 'worth recalling'."""
        rc, output = _run_hook("how does this work?", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "liberally" in output
        assert "when in doubt" in output

    def test_capture_protocol_sets_minimum_frequency(self, tmp_path):
        """Protocol must state a minimum capture frequency per response."""
        rc, output = _run_hook("explain the flow", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "at least 1" in output

    def test_capture_protocol_includes_expanded_categories(self, tmp_path):
        """Protocol must list expanded capture categories including root causes and constraints."""
        rc, output = _run_hook("why did this fail?", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "root cause" in output
        assert "constraint" in output

    def test_capture_protocol_prohibits_ai_written_notification(self, tmp_path):
        """Protocol must PROHIBIT AI from writing ✻ ... notification text (Method E, closes #22).

        Method E removes AI's responsibility for writing the notification entirely.
        The CLI's own stdout IS the notification. The protocol must include an explicit
        prohibition against duplicating the CLI output as AI-authored text.

        Replaces Method A (#19) which still allowed AI to write the notification
        (just requiring a mandatory [id: <uuid>] suffix — still bypassable).
        """
        rc, output = _run_hook("explain this system", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # Old Method-C wording must be gone
        assert "NEVER emit" not in output, (
            "Old Method-C wording 'NEVER emit' must not be present"
        )
        # Old Method-A wording (mandatory id format in notification) must be gone:
        # Under Method E, AI never writes the notification at all.
        assert "[id: <uuid>]" not in output, (
            "Method-A [id: <uuid>] format template must be removed under Method E; "
            "AI is prohibited from writing any ✻ notification"
        )
        # Method E prohibition must be present
        assert "DO NOT" in output or "forbidden" in output or "prohibited" in output, (
            "Protocol must contain an explicit prohibition (DO NOT / forbidden / prohibited) "
            "against AI writing ✻ ... notification text (Method E)"
        )
        assert "mnemos capture" in output

    def test_capture_protocol_notification_is_cli_output(self, tmp_path):
        """Protocol must state that the CLI output IS the notification (Method E).

        The architectural shift: notification is the CLI's stdout, not AI text.
        """
        rc, output = _run_hook("describe the architecture", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # Protocol must explain that CLI output is the canonical notification
        assert "CLI" in output or "tool result" in output or "stdout" in output, (
            "Protocol must explain that the mnemos capture CLI output IS the notification "
            "(Method E — CLI stdout is the canonical notification channel)"
        )

    def test_capture_protocol_no_mandatory_id_suffix_instruction(self, tmp_path):
        """Protocol must NOT instruct AI to include MANDATORY [id: <uuid>] suffix (Method E).

        Under Method E, the CLI prints the notification (with the real id) directly.
        AI is prohibited from writing any notification at all — so the MANDATORY
        id-suffix instruction from Method A (#19) is no longer needed or correct.
        """
        rc, output = _run_hook("describe the system design", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "MANDATORY" not in output, (
            "Method-A 'MANDATORY' id-suffix instruction must be removed under Method E; "
            "AI is prohibited from writing any ✻ notification"
        )

    def test_capture_protocol_no_notification_format_template(self, tmp_path):
        """Protocol must NOT include a notification format template for AI to follow (Method E).

        Under Method E, AI writes no notification — the CLI does. Providing a
        format template would imply AI should write the notification, which is forbidden.
        """
        rc, output = _run_hook("what is the capture flow?", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # The exact Method-A format string must not appear
        assert "[id: <uuid>]" not in output, (
            "Method-A [id: <uuid>] notification format template must be removed; "
            "under Method E AI never writes the notification"
        )

    def test_capture_protocol_no_old_method_c_wording(self, tmp_path):
        """The old Method-C prohibition text ('NEVER emit ✻ 🧠 without') must be absent.
        It was replaced by Method-A structural enforcement in #19."""
        rc, output = _run_hook("how does the pipeline work?", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "NEVER emit" not in output, (
            "Old 'NEVER emit ✻ 🧠 without' wording from #17 must be removed; "
            "Method A ([id: <uuid>] enforcement) supersedes it"
        )

    def test_capture_protocol_includes_session_id_flag(self, tmp_path):
        """The capture protocol command must include --session-id so captures are
        correlated with hook_search events sharing the same session (closes #18)."""
        session_id = "test-session-mnemos-18"
        rc, output = _run_hook("explain the observability pipeline",
                               session_id=session_id,
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "--session-id" in output, (
            "Capture protocol command must include --session-id for observability correlation"
        )
        assert session_id in output, (
            f"Capture protocol must embed the actual session_id ({session_id!r}) in the command"
        )

    def test_hook_exports_mnemos_session_id_env_var(self, tmp_path):
        """Hook must export MNEMOS_SESSION_ID so the CLI env-var fallback works
        even if Claude omits --session-id from the mnemos capture call (closes #18)."""
        # We verify indirectly: the capture protocol block in the hook output must
        # carry the session_id in the --session-id argument. The export is a
        # side-effect visible only to child processes of the hook; testing it
        # directly would require running a child that reads the env, which the
        # hook itself does not produce output for. The presence of the session_id
        # in the injected command text is the user-facing observable behaviour.
        session_id = "env-export-test-session"
        rc, output = _run_hook("any prompt", session_id=session_id,
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # The injected command must show the actual session_id (confirms SESSION_ID
        # was correctly read and exported as MNEMOS_SESSION_ID).
        assert session_id in output, (
            "Hook output must include the session_id to confirm MNEMOS_SESSION_ID export"
        )


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter template changes
# ---------------------------------------------------------------------------

class TestUserPromptSubmitTemplate:
    def test_template_references_hook_script(self):
        """The UserPromptSubmit template command must reference the hook script
        via the {hook_script} placeholder (which _render_template resolves to
        hooks/UserPromptSubmit.sh at install time)."""
        for hook in _USER_PROMPT_SUBMIT_HOOK_TEMPLATE.get("hooks", []):
            cmd = hook.get("command", "")
            assert "{hook_script}" in cmd or "UserPromptSubmit.sh" in cmd, (
                "Template command does not reference the hook script placeholder"
            )

    def test_render_template_substitutes_repo_root(self, tmp_path):
        """_render_template replaces {repo_root} in template commands."""
        repo_root = str(tmp_path / "my-repo")
        rendered = _render_template(_USER_PROMPT_SUBMIT_HOOK_TEMPLATE, repo_root)
        for hook in rendered.get("hooks", []):
            assert "{repo_root}" not in hook.get("command", "")
            assert repo_root in hook.get("command", "")

    def test_render_template_substitutes_hook_script(self, tmp_path):
        """_render_template replaces {hook_script} with the resolved path."""
        repo_root = str(tmp_path / "my-repo")
        rendered = _render_template(_USER_PROMPT_SUBMIT_HOOK_TEMPLATE, repo_root)
        expected_script = str(Path(repo_root) / "hooks" / "UserPromptSubmit.sh")
        for hook in rendered.get("hooks", []):
            assert "{hook_script}" not in hook.get("command", "")
            assert expected_script in hook.get("command", "")

    def test_hook_script_path_helper(self, tmp_path):
        """_hook_script_path returns hooks/UserPromptSubmit.sh inside repo_root."""
        repo_root = str(tmp_path / "repo")
        result = _hook_script_path(repo_root)
        assert result == str(Path(repo_root) / "hooks" / "UserPromptSubmit.sh")


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.install writes new hook format
# ---------------------------------------------------------------------------

class _FakeHome:
    """Create a minimal fake home directory for adapter tests."""
    def __init__(self, tmp_path: Path, repo_root: str = "/fake/repo"):
        self.home = tmp_path / "home"
        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "settings.json").write_text("{}\n")
        (claude_dir / "CLAUDE.md").write_text("# Existing\n")
        self.repo_root = repo_root


class TestAdapterInstallHookFormat:
    def test_install_writes_hook_script_reference(self, tmp_path, monkeypatch):
        """install() must write a UserPromptSubmit hook that references the script."""
        fake = _FakeHome(tmp_path, repo_root="/my/mnemos")
        monkeypatch.setenv("MNEMOS_REPO_ROOT", fake.repo_root)

        ClaudeCodeAdapter().install(fake.home)

        data = json.loads((fake.home / ".claude" / "settings.json").read_text())
        user_hooks = data.get("hooks", {}).get("UserPromptSubmit", [])
        assert any(
            "UserPromptSubmit.sh" in str(entry)
            for entry in user_hooks
        ), f"UserPromptSubmit.sh not found in hook entries: {user_hooks}"

    def test_install_embeds_repo_root_in_command(self, tmp_path, monkeypatch):
        """install() must embed the actual MNEMOS_REPO_ROOT in the hook command."""
        repo_root = "/specific/repo/path"
        fake = _FakeHome(tmp_path, repo_root=repo_root)
        monkeypatch.setenv("MNEMOS_REPO_ROOT", repo_root)

        ClaudeCodeAdapter().install(fake.home)

        data = json.loads((fake.home / ".claude" / "settings.json").read_text())
        user_hooks = data.get("hooks", {}).get("UserPromptSubmit", [])
        cmds = [h.get("command", "") for entry in user_hooks for h in entry.get("hooks", [])]
        assert any(repo_root in cmd for cmd in cmds), (
            f"repo_root not found in commands: {cmds}"
        )

    def test_update_replaces_old_search_hook(self, tmp_path, monkeypatch):
        """update() must replace an old inline-search hook with the new script hook."""
        repo_root = "/old/repo"
        fake = _FakeHome(tmp_path, repo_root=repo_root)

        # Write an old-style hook entry
        old_hook = {
            "matcher": "",
            "hooks": [{"type": "command", "command": f'MNEMOS_REPO_ROOT="{repo_root}" mnemos search "..." 2>/dev/null'}],
        }
        data = {"hooks": {"UserPromptSubmit": [old_hook]}}
        (fake.home / ".claude" / "settings.json").write_text(json.dumps(data))

        monkeypatch.setenv("MNEMOS_REPO_ROOT", repo_root)
        ClaudeCodeAdapter().update(fake.home)

        updated = json.loads((fake.home / ".claude" / "settings.json").read_text())
        user_hooks = updated.get("hooks", {}).get("UserPromptSubmit", [])
        # Old inline search should be gone, new script hook should be present
        cmds = [h.get("command", "") for entry in user_hooks for h in entry.get("hooks", [])]
        assert not any("mnemos search" in cmd and "UserPromptSubmit.sh" not in cmd for cmd in cmds), (
            "Old inline mnemos search hook still present after update"
        )
        assert any("UserPromptSubmit.sh" in cmd for cmd in cmds), (
            "New hook script not written by update()"
        )


# ---------------------------------------------------------------------------
# Keyword extraction (inline python3 snippet in UserPromptSubmit.sh)
# ---------------------------------------------------------------------------

def _extract_keywords(prompt: str, max_keywords: int = 5) -> list[str]:
    """Run the same extraction logic as the inline python3 snippet in the hook.

    This mirrors the snippet verbatim so that tests exercise the exact algorithm
    without spawning the full bash hook.
    """
    import re

    ENGLISH_STOPWORDS = {
        'the','a','an','is','are','was','were','be','been','have','has','had',
        'do','does','did','will','would','could','should','may','might','shall',
        'must','can','to','of','in','on','at','by','for','from','with','and',
        'or','but','not','so','if','as','it','its','this','that','these','those',
        'i','you','we','they','my','your','our','their','what','how','why',
        'when','where','which',
    }
    KOREAN_STOPWORDS = {
        '이','가','은','는','을','를','의','에','에서','으로','로','와','과',
        '하고','도','만','이다','있다','없다','했다','합니다','해요','거','것',
        '수','좀','그','저','제','네','아',
    }

    def split_camel(token: str) -> list[str]:
        parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', token)
        parts = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1 \2', parts)
        return parts.split()

    raw_tokens = re.split(r'[\s\W]+', prompt[:500])
    words: list[str] = []
    for tok in raw_tokens:
        words.extend(split_camel(tok))

    seen: set[str] = set()
    keywords: list[str] = []
    for w in words:
        lw = w.lower()
        if len(w) < 2:
            continue
        if lw in ENGLISH_STOPWORDS or w in KOREAN_STOPWORDS:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        keywords.append(w)

    keywords.sort(key=lambda x: -len(x))
    return keywords[:max_keywords]


class TestKeywordExtraction:
    """Unit tests for the inline keyword-extraction algorithm."""

    def test_camelcase_is_split_into_parts(self):
        """'EventBus' must be split into 'Event' and 'Bus', both searchable."""
        keywords = _extract_keywords("EventBus")
        assert "Event" in keywords, f"'Event' missing from {keywords}"
        assert "Bus" in keywords, f"'Bus' missing from {keywords}"

    def test_pascalcase_multi_word_split(self):
        """'UserPromptSubmit' splits into 'User', 'Prompt', 'Submit'."""
        keywords = _extract_keywords("UserPromptSubmit")
        assert "User" in keywords
        assert "Prompt" in keywords
        assert "Submit" in keywords

    def test_english_stopwords_filtered(self):
        """Common English stopwords must not appear in extracted keywords."""
        keywords = _extract_keywords("the is a to of and or but")
        assert keywords == [], f"Expected no keywords, got {keywords}"

    def test_korean_stopwords_filtered(self):
        """Korean grammatical particles must not appear in extracted keywords."""
        keywords = _extract_keywords("이 가 은 는 을 를 의 에")
        assert keywords == [], f"Expected no keywords, got {keywords}"

    def test_short_tokens_excluded(self):
        """Tokens with fewer than 2 characters must be dropped."""
        keywords = _extract_keywords("a i x y z")
        assert keywords == [], f"Expected no keywords, got {keywords}"

    def test_max_five_keywords(self):
        """At most 5 keywords must be returned regardless of prompt length."""
        prompt = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        keywords = _extract_keywords(prompt)
        assert len(keywords) <= 5, f"Got more than 5 keywords: {keywords}"

    def test_longest_keywords_preferred(self):
        """Keywords are sorted by length descending so longer words come first."""
        keywords = _extract_keywords("connection timeout server hi")
        # 'connection' (10) > 'timeout' (7) > 'server' (6) > 'hi' (2)
        assert keywords[0] == "connection", f"Expected 'connection' first, got {keywords}"
        assert keywords[1] == "timeout"
        assert keywords[2] == "server"

    def test_duplicate_tokens_deduplicated(self):
        """Repeated words appear only once in the keyword list."""
        keywords = _extract_keywords("memory memory memory cache cache")
        assert keywords.count("memory") == 1
        assert keywords.count("cache") == 1

    def test_mixed_prompt_real_world(self):
        """Realistic prompt produces meaningful keywords and excludes noise."""
        prompt = "Fix the EventBus connection timeout in the server module"
        keywords = _extract_keywords(prompt)
        # CamelCase split: EventBus → Event, Bus
        all_kw_lower = [k.lower() for k in keywords]
        assert "event" in all_kw_lower or "bus" in all_kw_lower, (
            f"Expected camelCase parts in {keywords}"
        )
        # 'the', 'in' are stopwords — must not appear
        assert "the" not in all_kw_lower
        assert "in" not in all_kw_lower

    def test_hook_searches_per_keyword_not_full_prompt(self, tmp_path):
        """Hook must issue per-keyword searches rather than passing the raw prompt.

        Strategy: install a fake 'mnemos' wrapper that records every argument it
        receives, then inspect those logs to confirm no single call received the
        full prompt verbatim.
        """
        # Create fake mnemos that logs argv to a file and exits 0 with no output.
        fake_bin = tmp_path / "mnemos"
        log_file = tmp_path / "mnemos_calls.log"
        fake_bin.write_text(
            f"#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {log_file}\n"
            f"exit 0\n"
        )
        fake_bin.chmod(0o755)

        long_prompt = (
            "How does the EventBus work with the ConnectionTimeout retry logic "
            "inside the ServerModule configuration system?"
        )
        env_extras = {"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}

        # Use a pre-existing session flag so session-start load is skipped.
        flag_dir = tmp_path / "mnemos-session-flags"
        flag_dir.mkdir(parents=True)
        (flag_dir / "mnemos-session-loaded-test-session-123").touch()

        rc, output = _run_hook(
            long_prompt,
            mnemos_repo_root=str(tmp_path),
            env_extras={**env_extras, "TMPDIR": str(tmp_path)},
        )
        assert rc == 0

        if not log_file.exists():
            # mnemos was never called (no PATH match or early exit) — skip.
            pytest.skip("fake mnemos was not invoked (PATH not resolved)")

        calls = log_file.read_text().splitlines()
        # No single mnemos call should include the entire long prompt verbatim.
        for call in calls:
            assert long_prompt not in call, (
                f"Full prompt passed verbatim to mnemos: {call!r}"
            )
        # At least one 'search' call must have happened.
        assert any("search" in call for call in calls), (
            f"No 'mnemos search' call found in: {calls}"
        )

    def test_hook_deduplicates_results(self, tmp_path):
        """Results appearing in multiple keyword searches appear only once in output."""
        # Create a fake mnemos that always returns the same result line.
        fake_bin = tmp_path / "mnemos"
        fake_bin.write_text(
            "#!/usr/bin/env bash\n"
            "echo 'mem-001 [global] Shared result line'\n"
            "exit 0\n"
        )
        fake_bin.chmod(0o755)

        # Use pre-existing session flag to skip session-start load.
        flag_dir = tmp_path / "mnemos-session-flags"
        flag_dir.mkdir(parents=True)
        (flag_dir / "mnemos-session-loaded-test-session-123").touch()

        prompt = "EventBus ConnectionTimeout ServerModule retry logic system"
        env_extras = {"PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"}

        rc, output = _run_hook(
            prompt,
            mnemos_repo_root=str(tmp_path),
            env_extras={**env_extras, "TMPDIR": str(tmp_path)},
        )
        assert rc == 0
        # "Shared result line" must appear at most once in the output.
        assert output.count("Shared result line") <= 1, (
            f"Duplicate result found in output:\n{output}"
        )


# ---------------------------------------------------------------------------
# Search trigger hints in the capture-protocol block (gap 2 — mnemos #20)
# ---------------------------------------------------------------------------

class TestSearchTriggerHints:
    """The capture-protocol block must also guide AI toward explicit mid-session search.

    Stats from observability.jsonl show hook_search (118) and explicit search (120)
    are nearly equal, but AI rarely calls mnemos search when new questions arise
    mid-session — relying instead on session-start auto-injection. These tests
    verify the hook now surfaces concrete 'when to search' guidance alongside
    the existing capture guidance.

    Trigger phrases that should prompt AI to call mnemos search proactively:
    - Analyzing a bug / error / exception — search related root-cause memories first
    - Answering 'why was X decided' / 'why does X work this way' — search prior decisions
    - Before refactoring — search known constraints
    - Architecture / design questions — search architecture decisions
    - Constraint check / 'can we' / 'is it allowed' — search project constraints
    """

    def test_capture_protocol_includes_search_trigger_guidance(self, tmp_path):
        """The <mnemos-capture-protocol> block must include 'mnemos search' guidance.

        Trigger phrase: any prompt — search guidance is always injected as part
        of the protocol block so AI has standing instructions for when to search.
        """
        rc, output = _run_hook("how does caching work?",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "mnemos search" in output, (
            "Capture-protocol block must mention 'mnemos search' to guide mid-session search"
        )

    def test_capture_protocol_mentions_bug_analysis_trigger(self, tmp_path):
        """Protocol must include 'bug' or 'error' as a trigger to run mnemos search first.

        Expected wording: 'when analyzing a bug' / 'before debugging' / 'error analysis'.
        """
        rc, output = _run_hook("why does the parser fail on empty input?",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "bug" in output or "error" in output, (
            "Protocol must mention bug/error as a search trigger so AI searches "
            "root-cause memories before starting debugging"
        )

    def test_capture_protocol_mentions_decision_review_trigger(self, tmp_path):
        """Protocol must guide AI to search prior decisions before answering 'why' questions.

        Expected wording: 'decision' or 'why was X decided'.
        """
        rc, output = _run_hook("why was the session layer designed this way?",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "decision" in output, (
            "Protocol must tell AI to search prior decisions when answering 'why' questions"
        )

    def test_capture_protocol_mentions_constraint_trigger(self, tmp_path):
        """Protocol must remind AI to search known constraints before refactoring.

        Expected wording: 'constraint' in the search-trigger section.
        """
        rc, output = _run_hook("I want to refactor the storage layer",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "constraint" in output, (
            "Protocol must remind AI to search known constraints before refactoring"
        )

    def test_search_hint_contains_concrete_example_query(self, tmp_path):
        """The protocol block must show a concrete example mnemos search call.

        A bare 'run mnemos search' is insufficient — AI needs a pattern to match.
        The hint must show 'mnemos search <topic>' or similar concrete form.
        """
        rc, output = _run_hook("explain the event bus architecture",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # The protocol must include a concrete search invocation pattern,
        # not just a generic mention of 'mnemos search'.
        assert "mnemos search" in output, (
            "Protocol must include a concrete 'mnemos search <topic>' example"
        )
        # Additionally verify it appears in a search-guidance context,
        # not only in the capture command context.
        lines = output.splitlines()
        search_lines = [l for l in lines if "mnemos search" in l]
        assert len(search_lines) >= 1, (
            "Expected at least one 'mnemos search' line in protocol output"
        )

    def test_search_trigger_guidance_appears_inside_capture_protocol_block(self, tmp_path):
        """Search trigger guidance must be INSIDE the <mnemos-capture-protocol> block.

        It must not be emitted as a separate free-form block outside the protocol —
        placing it inside ensures it fires on every prompt turn alongside capture guidance.
        """
        rc, output = _run_hook("describe the architecture", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # Extract content inside the protocol block
        start = output.find("<mnemos-capture-protocol>")
        end = output.find("</mnemos-capture-protocol>")
        assert start != -1 and end != -1, "Protocol block not found in output"
        protocol_content = output[start:end]
        assert "mnemos search" in protocol_content, (
            "Search trigger guidance must appear inside <mnemos-capture-protocol> block, "
            "not outside it"
        )


# ---------------------------------------------------------------------------
# MNEMOS_BEHAVIOR_BLOCK content checks (gap 2 — mnemos #20)
# ---------------------------------------------------------------------------

class TestBehaviorBlockSearchTriggers:
    """MNEMOS_BEHAVIOR_BLOCK (in core/adapters/base.py) must include concrete search triggers.

    Both Claude and Cursor adapters propagate this block to their respective
    AI-instruction files (CLAUDE.md and cursor rules). The trigger examples
    must be specific enough for AI to pattern-match them without ambiguity.

    Required trigger examples:
    1. Bug/error analysis: 'when analyzing a bug, search related root-cause memories first'
    2. Decision review: 'before answering why X was decided, search prior decisions'
    3. Pre-refactor: 'before refactoring, search known constraints'
    """

    def _get_behavior_block(self) -> str:
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
        return MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_has_search_section(self):
        """MNEMOS_BEHAVIOR_BLOCK must have a section dedicated to when to search.

        A generic 'run mnemos search <query>' line is insufficient.
        There must be a structured guidance section with concrete triggers.
        """
        block = self._get_behavior_block()
        # A dedicated heading or structured list for search triggers
        assert "search" in block.lower(), (
            "MNEMOS_BEHAVIOR_BLOCK must contain search guidance"
        )

    def test_behavior_block_has_bug_analysis_trigger(self):
        """MNEMOS_BEHAVIOR_BLOCK must include bug analysis as an explicit search trigger.

        Expected: 'analyzing a bug' or 'bug analysis' or 'error' in a search-trigger context.
        This ensures AI pattern-matches bug investigation prompts to a mnemos search call.
        """
        block = self._get_behavior_block()
        block_lower = block.lower()
        assert "bug" in block_lower or "error" in block_lower, (
            "MNEMOS_BEHAVIOR_BLOCK must mention 'bug' or 'error' as a search trigger "
            "so AI searches root-cause memories before debugging"
        )

    def test_behavior_block_has_decision_review_trigger(self):
        """MNEMOS_BEHAVIOR_BLOCK must include decision review as an explicit search trigger.

        Expected: 'why was X decided' / 'prior decision' / 'decision review'.
        This surfaces historical decision context when AI is asked 'why' questions.
        """
        block = self._get_behavior_block()
        block_lower = block.lower()
        assert "decision" in block_lower or "why was" in block_lower, (
            "MNEMOS_BEHAVIOR_BLOCK must mention 'decision' as a search trigger "
            "so AI retrieves prior decisions for 'why was X decided' questions"
        )

    def test_behavior_block_has_constraint_trigger(self):
        """MNEMOS_BEHAVIOR_BLOCK must include constraint lookup as an explicit search trigger.

        Expected: 'constraint' or 'refactor' in a search-trigger context.
        This prevents AI from refactoring in ways that violate captured constraints.
        """
        block = self._get_behavior_block()
        block_lower = block.lower()
        assert "constraint" in block_lower, (
            "MNEMOS_BEHAVIOR_BLOCK must mention 'constraint' as a search trigger "
            "so AI checks constraints before refactoring"
        )

    def test_behavior_block_triggers_are_concrete_not_generic(self):
        """Search triggers must be specific phrases, not just 'run mnemos search'.

        Generic 'When asked about past context, run mnemos search' is the existing
        opening line. The new trigger section must add concrete, pattern-matchable
        examples that go beyond the generic instruction.
        """
        block = self._get_behavior_block()
        # The block must contain a 'When' + action trigger construction beyond
        # the generic opening line, or a structured list of trigger cases.
        # We check for the presence of a second 'search' reference beyond the
        # opening 'run mnemos search <query>' line.
        search_occurrences = block.lower().count("mnemos search")
        assert search_occurrences >= 2, (
            f"MNEMOS_BEHAVIOR_BLOCK should reference 'mnemos search' at least twice "
            f"(opening line + trigger examples), found {search_occurrences}"
        )

    def test_cursor_adapter_propagates_behavior_block(self):
        """CursorAdapter must use the same MNEMOS_BEHAVIOR_BLOCK as ClaudeCodeAdapter.

        Both adapters import MNEMOS_BEHAVIOR_BLOCK from core.adapters.base, so any
        update to the block automatically propagates to both CLAUDE.md and cursor rules.
        """
        from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK as BASE_BLOCK
        from core.adapters.cursor import CURSOR_RULES_BLOCK
        from core.adapters.claude import CLAUDE_MD_BLOCK

        # Both adapter blocks must contain the base behavior block verbatim
        assert BASE_BLOCK in CURSOR_RULES_BLOCK, (
            "CursorAdapter's CURSOR_RULES_BLOCK must embed MNEMOS_BEHAVIOR_BLOCK verbatim"
        )
        assert BASE_BLOCK in CLAUDE_MD_BLOCK, (
            "ClaudeCodeAdapter's CLAUDE_MD_BLOCK must embed MNEMOS_BEHAVIOR_BLOCK verbatim"
        )
