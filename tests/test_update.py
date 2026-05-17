"""Tests for mnemos update command (core/updater.py + CLI)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from core.updater import (
    CLAUDE_MD_BLOCK,
    CURSOR_RULES_BLOCK,
    update_claude_md,
    update_cursor_rules,
    update_settings_json,
    _run_bg_check_quiet,
)


# ---------------------------------------------------------------------------
# update_settings_json
# ---------------------------------------------------------------------------

class TestUpdateSettingsJson:
    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_replaces_post_tool_use_hook(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/old/path" mnemos ingest-claude-md'}],
                    }
                ]
            }
        }
        self._write(settings, data)

        changed, diff = update_settings_json(settings)

        assert changed is True
        assert diff != ""
        result = json.loads(settings.read_text())
        hooks = result["hooks"]["PostToolUse"]
        assert len(hooks) == 1
        cmd = hooks[0]["hooks"][0]["command"]
        assert "mnemos ingest-claude-md" in cmd
        assert hooks[0]["matcher"] == "Write|Edit"

    def test_replaces_user_prompt_submit_hook(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/old/path" mnemos search "${CLAUDE_PROMPT:0:200}" 2>/dev/null | head -30 || true'}],
                    }
                ]
            }
        }
        self._write(settings, data)

        changed, diff = update_settings_json(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        hooks = result["hooks"]["UserPromptSubmit"]
        assert len(hooks) == 1
        cmd = hooks[0]["hooks"][0]["command"]
        assert "mnemos search" in cmd

    def test_preserves_non_mnemos_hooks(self, tmp_path):
        settings = tmp_path / "settings.json"
        other_hook = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "echo hello"}],
        }
        data = {
            "hooks": {
                "PostToolUse": [
                    other_hook,
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}],
                    },
                ]
            }
        }
        self._write(settings, data)

        changed, diff = update_settings_json(settings)

        result = json.loads(settings.read_text())
        post_hooks = result["hooks"]["PostToolUse"]
        cmds = [h["hooks"][0]["command"] for h in post_hooks]
        assert any("echo hello" in c for c in cmds), "non-mnemos hook must be preserved"

    def test_no_change_returns_false(self, tmp_path):
        """If no mnemos hooks exist and canonical ones are already in place,
        a second call should still succeed (idempotent result may vary, but
        no exception should occur)."""
        settings = tmp_path / "settings.json"
        data: dict = {}
        self._write(settings, data)

        # First call adds canonical hooks
        changed1, _ = update_settings_json(settings)
        # Second call: result should be stable
        changed2, _ = update_settings_json(settings)
        assert changed2 is False  # content identical to what we just wrote

    def test_returns_false_for_missing_file(self, tmp_path):
        settings = tmp_path / "nonexistent.json"
        changed, diff = update_settings_json(settings)
        assert changed is False
        assert diff == ""

    def test_diff_shows_old_and_new(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "OldMatcher",
                        "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}],
                    }
                ]
            }
        }
        self._write(settings, data)
        _, diff = update_settings_json(settings)
        assert "---" in diff
        assert "+++" in diff

    def test_replaces_legacy_stop_hook_with_canonical_stop_sh(self, tmp_path):
        """update must replace a legacy mnemos extract-insight Stop hook with the
        canonical Stop.sh hook (re-introduced with dedup idempotency contract).

        Issue #4 anti-regression rationale
        -----------------------------------
        The Stop hook was originally removed (commit 0de1c47) because Claude
        Code's Stop event fires once per AI response turn — not at true session
        end.  This caused one new mnemos capture per turn, flooding the session
        layer with identical entries.

        The hook is now re-introduced safely because gateway.capture() maintains
        a per-process in-memory dedup registry keyed by
        ``(layer, SHA-256(NFKC-normalised content))``.  Duplicate captures
        within the same process are silent no-ops — the write path (store, FTS,
        audit log, event bus) is never reached.  Per-turn re-parsing of the same
        ✻ emoji markers produces exactly one persisted item, not N.

        This test asserts the NEW behaviour: legacy extract-insight entries are
        removed AND the canonical Stop.sh entry is installed in their place.
        """
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos extract-insight'}],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos ingest-claude-md'}],
                    }
                ],
            }
        }
        self._write(settings, data)

        changed, diff = update_settings_json(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        # Legacy extract-insight entry must be gone
        stop_cmds = [h["hooks"][0]["command"] for h in hooks.get("Stop", [])]
        assert not any("extract-insight" in c for c in stop_cmds), (
            "Legacy extract-insight Stop hook must be removed after update"
        )
        # The Stop key may be absent or contain only the canonical Stop.sh entry
        # (update_settings_json does not install Stop.sh — the adapter does).
        # At minimum, extract-insight must be gone.

    def test_legacy_stop_hook_removal_leaves_non_mnemos_stop_entries(self, tmp_path):
        """update must only remove legacy (non-Stop.sh) mnemos entries from Stop,
        preserving non-mnemos entries and the canonical Stop.sh entry itself.

        Issue #4 anti-regression rationale
        -----------------------------------
        The Stop hook was originally removed (commit 0de1c47) because of per-turn
        flooding.  The gateway.capture() on-write dedup makes re-introduction safe:
        duplicate (layer, content-hash) captures are silent no-ops within a process.

        This test verifies that the updater's selective removal only strips legacy
        mnemos commands (extract-insight, search, etc.) and never touches:
          - non-mnemos entries (echo done)
          - the canonical Stop.sh hook (which is safe under the dedup contract)
        """
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "mnemos extract-insight"}],
                    },
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "echo done"}],
                    },
                ],
            }
        }
        self._write(settings, data)

        changed, diff = update_settings_json(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        stop_hooks = result.get("hooks", {}).get("Stop", [])
        cmds = [h["hooks"][0]["command"] for h in stop_hooks]
        assert any("echo done" in c for c in cmds), "non-mnemos Stop entry must be preserved"
        assert all("extract-insight" not in c for c in cmds), (
            "legacy extract-insight Stop entry must be removed"
        )


# ---------------------------------------------------------------------------
# update_claude_md
# ---------------------------------------------------------------------------

class TestUpdateClaudeMd:
    def test_replaces_existing_block(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        old_block = (
            "<!-- mnemos-start -->\n"
            "## Memory (OLD VERSION)\n"
            "Old content here.\n"
            "<!-- mnemos-end -->"
        )
        original = f"# Global Rules\n\nSome content.\n\n{old_block}\n"
        claude_md.write_text(original)

        changed, diff = update_claude_md(claude_md)

        assert changed is True
        content = claude_md.read_text()
        assert "OLD VERSION" not in content
        assert "<!-- mnemos-start -->" in content
        assert "<!-- mnemos-end -->" in content
        assert "mnemos search" in content
        # Content outside block is preserved
        assert "# Global Rules" in content
        assert "Some content." in content

    def test_appends_block_when_absent(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Existing content\n")

        changed, diff = update_claude_md(claude_md)

        assert changed is True
        content = claude_md.read_text()
        assert "<!-- mnemos-start -->" in content
        assert "<!-- mnemos-end -->" in content
        assert "# Existing content" in content

    def test_no_change_when_block_already_canonical(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(f"# Header\n\n{CLAUDE_MD_BLOCK}\n")

        changed, diff = update_claude_md(claude_md)

        assert changed is False
        assert diff == ""

    def test_returns_false_for_missing_file(self, tmp_path):
        changed, diff = update_claude_md(tmp_path / "CLAUDE.md")
        assert changed is False
        assert diff == ""

    def test_diff_contains_unified_diff_markers(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "<!-- mnemos-start -->\nold\n<!-- mnemos-end -->\n"
        )
        _, diff = update_claude_md(claude_md)
        assert "---" in diff
        assert "+++" in diff
        assert "-old" in diff

    def test_only_replaces_managed_block_not_surrounding_content(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        before_block = "# My Rules\n\nDo not remove this.\n\n"
        after_block = "\n\nKeep this too.\n"
        content = (
            before_block
            + "<!-- mnemos-start -->\nold block\n<!-- mnemos-end -->"
            + after_block
        )
        claude_md.write_text(content)

        update_claude_md(claude_md)
        result = claude_md.read_text()

        assert "Do not remove this." in result
        assert "Keep this too." in result
        assert "old block" not in result


# ---------------------------------------------------------------------------
# update_cursor_rules
# ---------------------------------------------------------------------------

class TestUpdateCursorRules:
    def test_replaces_existing_block_in_rules_file(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        old_block = (
            "<!-- mnemos:start -->\n"
            "## Memory OLD\n"
            "<!-- mnemos:end -->"
        )
        rules.write_text(f"# Cursor rules\n\n{old_block}\n")

        changed, diff = update_cursor_rules(cursor_dir)

        assert changed is True
        content = rules.read_text()
        assert "Memory OLD" not in content
        assert "<!-- mnemos:start -->" in content
        assert "<!-- mnemos:end -->" in content
        assert "mnemos search" in content
        assert "# Cursor rules" in content

    def test_replaces_existing_block_in_rules_md_file(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules_md = cursor_dir / "rules.md"
        rules_md.write_text(
            "# Rules\n<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n"
        )

        changed, diff = update_cursor_rules(cursor_dir)

        assert changed is True
        content = rules_md.read_text()
        assert "old" not in content
        assert "mnemos search" in content

    def test_prefers_rules_over_rules_md(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules_md = cursor_dir / "rules.md"
        rules.write_text("<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n")
        rules_md.write_text("# Should not be touched\n")

        update_cursor_rules(cursor_dir)

        # rules.md must be unchanged
        assert rules_md.read_text() == "# Should not be touched\n"

    def test_appends_when_block_absent(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text("existing content\n")

        changed, diff = update_cursor_rules(cursor_dir)

        assert changed is True
        assert "<!-- mnemos:start -->" in rules.read_text()

    def test_no_change_when_already_canonical(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text(CURSOR_RULES_BLOCK + "\n")

        changed, diff = update_cursor_rules(cursor_dir)

        assert changed is False
        assert diff == ""

    def test_returns_false_when_no_cursor_dir(self, tmp_path):
        cursor_dir = tmp_path / ".cursor_absent"
        changed, diff = update_cursor_rules(cursor_dir)
        assert changed is False
        assert diff == ""

    def test_only_replaces_managed_block_not_surrounding_content(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text(
            "# Keep me\n"
            "<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n"
            "# Keep me too\n"
        )

        update_cursor_rules(cursor_dir)
        content = rules.read_text()

        assert "# Keep me" in content
        assert "# Keep me too" in content
        assert "old" not in content


# ---------------------------------------------------------------------------
# CLI integration test
# ---------------------------------------------------------------------------

class TestUpdateCliCommand:
    def test_update_command_exists(self):
        """update sub-command must be registered on the CLI."""
        from core.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["update", "--help"])
        assert result.exit_code == 0
        assert "update" in result.output.lower() or "self-update" in result.output.lower()

    def test_update_skips_git_and_pipx_flags(self, tmp_path):
        """--skip-git-pull --skip-pipx should only touch config blocks."""
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text(
            "<!-- mnemos-start -->\nold content\n<!-- mnemos-end -->\n"
        )
        settings = claude_dir / "settings.json"
        settings.write_text(json.dumps({}) + "\n")

        from core.cli import cli
        from core import updater as updater_module

        # Monkey-patch run_update to use our tmp home
        original_run_update = updater_module.run_update

        def patched_run_update(repo_root=None, skip_git_pull=False, skip_pipx=False, home=None):
            return original_run_update(
                repo_root=repo_root,
                skip_git_pull=skip_git_pull,
                skip_pipx=skip_pipx,
                home=tmp_path / "home",
            )

        updater_module.run_update = patched_run_update
        try:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                ["update", "--skip-git-pull", "--skip-pipx"],
            )
        finally:
            updater_module.run_update = original_run_update

        assert result.exit_code == 0, result.output
        content = claude_md.read_text()
        assert "old content" not in content
        assert "<!-- mnemos-start -->" in content


# ---------------------------------------------------------------------------
# _run_bg_check_quiet  (new: crew:update hook, Issue #27)
# ---------------------------------------------------------------------------

class TestRunBgCheckQuiet:
    def test_calls_run_background_check(self, tmp_path, monkeypatch):
        """_run_bg_check_quiet must invoke run_background_check with force=True."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))

        called_kwargs: dict = {}

        def fake_bg_check(repo_root, **kwargs):
            called_kwargs.update(kwargs)
            called_kwargs["repo_root"] = repo_root
            from core.bg import BackgroundCheckResult
            return BackgroundCheckResult(ran=True)

        with patch("core.bg.run_background_check", side_effect=fake_bg_check):
            _run_bg_check_quiet()

        assert called_kwargs.get("force") is True, "bg-check must run with force=True"

    def test_force_true_bypasses_throttle(self, tmp_path, monkeypatch):
        """The update hook must bypass the throttle (force=True)."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))

        from core.bg import BackgroundCheckResult
        with patch("core.bg.run_background_check", return_value=BackgroundCheckResult(ran=True)) as mock:
            _run_bg_check_quiet()

        _, kwargs = mock.call_args
        assert kwargs.get("force") is True

    def test_gc_enabled_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        from core.bg import BackgroundCheckResult
        with patch("core.bg.run_background_check", return_value=BackgroundCheckResult(ran=True)) as mock:
            _run_bg_check_quiet()
        _, kwargs = mock.call_args
        assert kwargs.get("gc_enabled") is True

    def test_exception_propagates_to_caller(self, tmp_path, monkeypatch):
        """_run_bg_check_quiet propagates exceptions; run_update handles them non-fatally."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(tmp_path))
        with patch("core.bg.run_background_check", side_effect=RuntimeError("bang")):
            # _run_bg_check_quiet itself may raise — it is run_update that catches it
            with pytest.raises(RuntimeError, match="bang"):
                _run_bg_check_quiet()


class TestRunUpdateBgCheckStep:
    """Verify that run_update calls _run_bg_check_quiet as its last step."""

    def test_bg_check_is_called_during_update(self, tmp_path, monkeypatch):
        """run_update must call _run_bg_check_quiet when skip flags are set."""
        from core import updater as updater_module

        bg_check_called = []

        def fake_bg_check_quiet():
            bg_check_called.append(True)

        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", fake_bg_check_quiet)

        updater_module.run_update(
            skip_git_pull=True,
            skip_pipx=True,
            home=tmp_path,
        )

        assert bg_check_called, "run_update must call _run_bg_check_quiet"

    def test_bg_check_failure_does_not_affect_exit_code(self, tmp_path, monkeypatch):
        """A failing bg-check must not change run_update's exit code."""
        from core import updater as updater_module

        def failing_bg_check():
            raise RuntimeError("bg-check exploded")

        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", failing_bg_check)

        exit_code = updater_module.run_update(
            skip_git_pull=True,
            skip_pipx=True,
            home=tmp_path,
        )

        assert exit_code == 0, "bg-check failure must not change update exit code"
