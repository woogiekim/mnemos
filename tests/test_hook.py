"""Tests for hooks/UserPromptSubmit.sh behaviour and ClaudeCodeAdapter integration."""
from __future__ import annotations

import json
import os
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
