"""Tests for HookDispatcher in mnemos/hooks.py."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.hooks import HookDispatcher, VALID_EVENTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hook_script(hooks_dir: Path, event: str, script_name: str, content: str) -> Path:
    """Create a hook script file and make it executable."""
    event_dir = hooks_dir / event
    event_dir.mkdir(parents=True, exist_ok=True)
    script = event_dir / script_name
    script.write_text(content)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHookDispatcherInit:
    def test_init_sets_root(self, tmp_path):
        dispatcher = HookDispatcher(str(tmp_path))
        assert dispatcher._root == tmp_path

    def test_init_sets_hooks_dir(self, tmp_path):
        dispatcher = HookDispatcher(str(tmp_path))
        assert dispatcher._hooks_dir == tmp_path / ".agent" / "workflows" / "hooks"


class TestHookDispatcherFire:
    def test_fire_does_nothing_when_event_dir_missing(self, tmp_path):
        dispatcher = HookDispatcher(str(tmp_path))
        # Should not raise even though the directory does not exist
        dispatcher.fire("post-capture", {"key": "value"})

    def test_fire_skips_non_executable_non_script_files(self, tmp_path):
        hooks_dir = tmp_path / ".agent" / "workflows" / "hooks"
        event_dir = hooks_dir / "post-capture"
        event_dir.mkdir(parents=True)
        # A plain .txt file — should be ignored
        (event_dir / "notes.txt").write_text("ignore me")

        with patch("subprocess.Popen") as mock_popen:
            HookDispatcher(str(tmp_path)).fire("post-capture", {})
            mock_popen.assert_not_called()


class TestRunScriptUsesDevNull:
    """Verify that _run_script passes stderr=DEVNULL, not PIPE."""

    def test_stderr_is_devnull(self, tmp_path):
        """subprocess.Popen must be called with stderr=subprocess.DEVNULL."""
        hooks_dir = tmp_path / ".agent" / "workflows" / "hooks"
        script = _make_hook_script(
            hooks_dir,
            event="post-capture",
            script_name="00-test.sh",
            content="#!/bin/sh\nexit 0\n",
        )

        captured_kwargs: dict = {}

        def fake_popen(args, **kwargs):
            captured_kwargs.update(kwargs)
            mock = MagicMock()
            return mock

        with patch("subprocess.Popen", side_effect=fake_popen):
            HookDispatcher(str(tmp_path)).fire("post-capture", {"mem_id": "abc"})

        assert "stderr" in captured_kwargs, "stderr kwarg was not passed to Popen"
        assert captured_kwargs["stderr"] == subprocess.DEVNULL, (
            f"Expected stderr=DEVNULL, got {captured_kwargs['stderr']!r}"
        )

    def test_stdout_is_devnull(self, tmp_path):
        """stdout should also be discarded."""
        hooks_dir = tmp_path / ".agent" / "workflows" / "hooks"
        _make_hook_script(
            hooks_dir,
            event="post-capture",
            script_name="00-test.sh",
            content="#!/bin/sh\nexit 0\n",
        )

        captured_kwargs: dict = {}

        def fake_popen(args, **kwargs):
            captured_kwargs.update(kwargs)
            return MagicMock()

        with patch("subprocess.Popen", side_effect=fake_popen):
            HookDispatcher(str(tmp_path)).fire("post-capture", {})

        assert captured_kwargs.get("stdout") == subprocess.DEVNULL


class TestNoDeadlockOnLargeStderr:
    """Integration-level test: a script emitting large stderr must not block."""

    @pytest.mark.timeout(10)
    def test_large_stderr_does_not_deadlock(self, tmp_path):
        """
        A hook that writes 100 000 bytes to stderr must not deadlock.
        With stderr=PIPE and no consumer this would stall; with DEVNULL it completes.
        """
        hooks_dir = tmp_path / ".agent" / "workflows" / "hooks"
        _make_hook_script(
            hooks_dir,
            event="post-capture",
            script_name="10-noisy.sh",
            # Write a large amount to stderr then exit cleanly
            content=(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('x' * 100_000)\n"
            ),
        )

        dispatcher = HookDispatcher(str(tmp_path))
        # This must return without hanging
        dispatcher.fire("post-capture", {"test": "large_stderr"})

    def test_env_vars_forwarded(self, tmp_path):
        """payload keys are forwarded as MNEMOS_* env vars."""
        hooks_dir = tmp_path / ".agent" / "workflows" / "hooks"
        _make_hook_script(
            hooks_dir,
            event="post-promote",
            script_name="00-env.sh",
            content="#!/bin/sh\nexit 0\n",
        )

        captured_env: dict = {}

        def fake_popen(args, env=None, **kwargs):
            if env:
                captured_env.update(env)
            return MagicMock()

        with patch("subprocess.Popen", side_effect=fake_popen):
            HookDispatcher(str(tmp_path)).fire("post-promote", {"mem_id": "xyz", "level": "2"})

        assert captured_env.get("MNEMOS_MEM_ID") == "xyz"
        assert captured_env.get("MNEMOS_LEVEL") == "2"
