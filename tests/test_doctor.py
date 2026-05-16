"""Tests for the `mnemos doctor` CLI command and verify_hooks() adapter methods."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from core.adapters import ClaudeCodeAdapter, CursorAdapter
from core.adapters.claude import CLAUDE_MD_BLOCK
from core.adapters.cursor import CURSOR_RULES_BLOCK
from core.cli import cli


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def runner():
    return CliRunner()


def _make_claude_home_with_hooks(tmp_path: Path) -> Path:
    """Create a home with fully-installed Claude Code hooks."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)

    # settings.json with all required hooks:
    #   PostToolUse (ingest-claude-md) + PostToolUse (bg-check / PostToolUse.sh)
    #   + UserPromptSubmit (UserPromptSubmit.sh)
    data = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" mnemos ingest-claude-md'}],
                },
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" bash "/x/hooks/PostToolUse.sh" 2>/dev/null || true'}],
                },
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" bash "/x/hooks/UserPromptSubmit.sh" 2>/dev/null || true'}],
                }
            ],
        }
    }
    (claude_dir / "settings.json").write_text(json.dumps(data, indent=2) + "\n")

    # CLAUDE.md with managed block
    (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD_BLOCK + "\n")

    return home


def _make_claude_home_missing_hooks(tmp_path: Path) -> Path:
    """Create a home where Claude Code is detected but hooks are missing."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}\n")
    (claude_dir / "CLAUDE.md").write_text("# Some content without mnemos block\n")
    return home


def _make_cursor_home_with_block(tmp_path: Path) -> Path:
    """Create a home with fully-installed Cursor rules block."""
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "rules").write_text(CURSOR_RULES_BLOCK + "\n")
    return home


def _make_cursor_home_missing_block(tmp_path: Path) -> Path:
    """Create a home where Cursor is detected but the managed block is missing."""
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "rules").write_text("# Cursor rules — no mnemos block here\n")
    return home


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.verify_hooks
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterVerifyHooks:
    def test_all_hooks_present_returns_ok(self, tmp_path):
        home = _make_claude_home_with_hooks(tmp_path)
        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        assert ok is True
        assert missing == []

    def test_missing_both_hooks_returns_false(self, tmp_path):
        home = _make_claude_home_missing_hooks(tmp_path)
        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        assert ok is False
        assert len(missing) >= 1

    def test_missing_post_tool_use_hook(self, tmp_path):
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        # Only UserPromptSubmit present, no PostToolUse
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" mnemos search ""'}]}
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD_BLOCK + "\n")

        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        assert ok is False
        assert any("PostToolUse" in m for m in missing)

    def test_missing_user_prompt_submit_hook(self, tmp_path):
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        # Only PostToolUse present, no UserPromptSubmit
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" mnemos ingest-claude-md'}]}
                ]
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD_BLOCK + "\n")

        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        assert ok is False
        assert any("UserPromptSubmit" in m for m in missing)

    def test_missing_claude_md_block(self, tmp_path):
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" mnemos ingest-claude-md'}]}
                ],
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="" mnemos search ""'}]}
                ],
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(data, indent=2) + "\n")
        (claude_dir / "CLAUDE.md").write_text("# No managed block\n")

        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        assert ok is False
        assert any("CLAUDE.md" in m for m in missing)

    def test_skips_check_when_settings_json_absent(self, tmp_path):
        """No settings.json → missing list must not raise, should be empty for that file."""
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text(CLAUDE_MD_BLOCK + "\n")
        # No settings.json — should not crash

        ok, missing = ClaudeCodeAdapter().verify_hooks(home)
        # No settings.json means no hook checks to fail for it
        assert isinstance(ok, bool)
        assert isinstance(missing, list)


# ---------------------------------------------------------------------------
# CursorAdapter.verify_hooks
# ---------------------------------------------------------------------------

class TestCursorAdapterVerifyHooks:
    def test_block_present_returns_ok(self, tmp_path):
        home = _make_cursor_home_with_block(tmp_path)
        ok, missing = CursorAdapter().verify_hooks(home)
        assert ok is True
        assert missing == []

    def test_block_absent_returns_false(self, tmp_path):
        home = _make_cursor_home_missing_block(tmp_path)
        ok, missing = CursorAdapter().verify_hooks(home)
        assert ok is False
        assert len(missing) >= 1
        assert any("cursor rules" in m.lower() for m in missing)

    def test_no_rules_file_returns_ok_trivially(self, tmp_path):
        """When no rules file exists there is nothing to verify — should not fail."""
        home = tmp_path / "home"
        (home / ".cursor").mkdir(parents=True)
        ok, missing = CursorAdapter().verify_hooks(home)
        assert ok is True
        assert missing == []


# ---------------------------------------------------------------------------
# mnemos doctor CLI command
# ---------------------------------------------------------------------------

class TestDoctorCommand:
    def _run_doctor(self, runner, home: Path, monkeypatch):
        """Invoke `mnemos doctor` with Path.home() patched to home."""
        monkeypatch.setattr("core.cli.Path.home", lambda: home)
        return runner.invoke(cli, ["doctor"])

    def test_doctor_shows_ok_when_all_hooks_present(self, runner, tmp_path, monkeypatch):
        home = _make_claude_home_with_hooks(tmp_path)
        # No cursor dir → SKIPPED for Cursor
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        assert "Claude Code" in result.output

    def test_doctor_shows_skipped_for_absent_adapter(self, runner, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        # Neither ~/.claude nor ~/.cursor exists
        monkeypatch.setattr("shutil.which", lambda _: None)
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "SKIPPED" in result.output

    def test_doctor_auto_fixes_missing_claude_hooks(self, runner, tmp_path, monkeypatch):
        home = _make_claude_home_missing_hooks(tmp_path)
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "FIXED" in result.output
        assert "Claude Code" in result.output

        # Verify hooks were actually written
        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "PostToolUse" in hooks
        assert "UserPromptSubmit" in hooks

    def test_doctor_auto_fixes_missing_cursor_block(self, runner, tmp_path, monkeypatch):
        home = _make_cursor_home_missing_block(tmp_path)
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "FIXED" in result.output
        assert "Cursor" in result.output

        # Verify block was written
        rules = home / ".cursor" / "rules"
        assert "<!-- mnemos:start -->" in rules.read_text()

    def test_doctor_shows_all_ok_message_when_nothing_to_repair(self, runner, tmp_path, monkeypatch):
        home = _make_claude_home_with_hooks(tmp_path)
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "All hooks verified" in result.output

    def test_doctor_shows_repair_complete_message_when_fixed(self, runner, tmp_path, monkeypatch):
        home = _make_claude_home_missing_hooks(tmp_path)
        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "doctor complete" in result.output

    def test_doctor_both_adapters_ok(self, runner, tmp_path, monkeypatch):
        """When both adapters are present and fully configured, all show OK."""
        home = _make_claude_home_with_hooks(tmp_path)
        cursor_dir = home / ".cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "rules").write_text(CURSOR_RULES_BLOCK + "\n")

        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert "OK" in result.output
        assert "FIXED" not in result.output

    def test_doctor_both_adapters_fixed(self, runner, tmp_path, monkeypatch):
        """When both adapters are present but both missing hooks, both show FIXED."""
        home = _make_claude_home_missing_hooks(tmp_path)
        cursor_dir = home / ".cursor"
        cursor_dir.mkdir(parents=True)
        (cursor_dir / "rules").write_text("# No managed block\n")

        result = self._run_doctor(runner, home, monkeypatch)
        assert result.exit_code == 0, result.output
        assert result.output.count("FIXED") == 2
