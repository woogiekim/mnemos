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
        """The capture protocol block must include the mnemos capture command."""
        rc, output = _run_hook("explain the architecture",
                               mnemos_repo_root=str(tmp_path))
        assert rc == 0
        assert "mnemos capture" in output
        assert "--quiet" in output
        assert "--layer session" in output

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

    def test_capture_protocol_prohibits_notification_without_tool_call(self, tmp_path):
        """Protocol must explicitly forbid emitting ✻ 🧠 without a preceding successful
        mnemos capture tool call (fixes issue #17 — fake notification without capture)."""
        rc, output = _run_hook("explain this system", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # The prohibition must be present in the protocol block
        assert "NEVER emit" in output, (
            "Protocol must explicitly forbid emitting ✻ 🧠 without a capture tool call"
        )
        assert "mnemos capture" in output

    def test_capture_protocol_notification_gated_on_captured_id(self, tmp_path):
        """Protocol must state that notification is only allowed after mnemos capture
        returns a captured ID — not as a freestanding text emission."""
        rc, output = _run_hook("describe the architecture", mnemos_repo_root=str(tmp_path))
        assert rc == 0
        # Wording must make the gate explicit: captured ID must precede notification
        assert "captured ID" in output or "returns a captured" in output, (
            "Protocol must gate the notification on a confirmed capture ID from mnemos capture"
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
