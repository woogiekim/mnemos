"""Tests for mnemos uninstall command (core/uninstaller.py + CLI)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from core.uninstaller import (
    _read_pipx_metadata,
    purge_mnemos_artifacts,
    remove_claude_md_block,
    remove_cursor_rules_block,
    remove_settings_json_hooks,
    remove_zshrc_line,
    run_uninstall,
)


# ---------------------------------------------------------------------------
# remove_settings_json_hooks
# ---------------------------------------------------------------------------

class TestRemoveSettingsJsonHooks:
    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, indent=2) + "\n")

    def test_removes_post_tool_use_hook(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos ingest-claude-md'}],
                    }
                ]
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is True
        assert diff != ""
        result = json.loads(settings.read_text())
        # hooks key should be absent or PostToolUse should be empty/absent
        hooks = result.get("hooks", {})
        assert "PostToolUse" not in hooks

    def test_removes_user_prompt_submit_hook(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos search "${CLAUDE_PROMPT:0:200}" 2>/dev/null | head -30 || true'}],
                    }
                ]
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "UserPromptSubmit" not in hooks

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

        changed, diff = remove_settings_json_hooks(settings)

        result = json.loads(settings.read_text())
        post_hooks = result["hooks"]["PostToolUse"]
        cmds = [h["hooks"][0]["command"] for h in post_hooks]
        assert any("echo hello" in c for c in cmds), "non-mnemos hook must be preserved"
        assert all("mnemos ingest-claude-md" not in c for c in cmds), "mnemos hook must be removed"

    def test_removes_both_hook_types_in_one_pass(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                ],
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'mnemos search "${CLAUDE_PROMPT:0:200}"'}]}
                ],
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "PostToolUse" not in hooks
        assert "UserPromptSubmit" not in hooks

    def test_no_change_when_no_mnemos_hooks(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
                ]
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is False
        assert diff == ""

    def test_returns_false_for_missing_file(self, tmp_path):
        settings = tmp_path / "nonexistent.json"
        changed, diff = remove_settings_json_hooks(settings)
        assert changed is False
        assert diff == ""

    def test_diff_shows_removal_markers(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                ]
            }
        }
        self._write(settings, data)
        _, diff = remove_settings_json_hooks(settings)
        assert "---" in diff
        assert "+++" in diff

    def test_hooks_key_removed_when_empty(self, tmp_path):
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                ]
            }
        }
        self._write(settings, data)

        remove_settings_json_hooks(settings)

        result = json.loads(settings.read_text())
        assert "hooks" not in result

    def test_removes_stop_hook_with_mnemos_command(self, tmp_path):
        """uninstall must remove legacy Stop hook containing any mnemos command."""
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "Stop": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos extract-insight'}],
                    }
                ]
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is True
        assert diff != ""
        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "Stop" not in hooks, "Stop hook must be removed by uninstall"

    def test_removes_all_hook_types_including_stop(self, tmp_path):
        """uninstall removes PostToolUse, UserPromptSubmit, and Stop in one pass."""
        settings = tmp_path / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                ],
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'mnemos search "${CLAUDE_PROMPT:0:200}"'}]}
                ],
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "mnemos extract-insight"}]}
                ],
            }
        }
        self._write(settings, data)

        changed, diff = remove_settings_json_hooks(settings)

        assert changed is True
        result = json.loads(settings.read_text())
        assert "hooks" not in result, "all hook types must be removed, leaving no hooks key"


# ---------------------------------------------------------------------------
# remove_claude_md_block
# ---------------------------------------------------------------------------

class TestRemoveClaudeMdBlock:
    def test_removes_existing_block(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        block = (
            "<!-- mnemos-start -->\n"
            "## Memory (mnemos)\n"
            "When asked about past context, run: `mnemos search <query>`\n"
            "<!-- mnemos-end -->"
        )
        claude_md.write_text(f"# Global Rules\n\nSome content.\n\n{block}\n")

        changed, diff = remove_claude_md_block(claude_md)

        assert changed is True
        content = claude_md.read_text()
        assert "<!-- mnemos-start -->" not in content
        assert "<!-- mnemos-end -->" not in content
        assert "mnemos search" not in content
        # Surrounding content is preserved
        assert "# Global Rules" in content
        assert "Some content." in content

    def test_preserves_content_outside_block(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Rules\n\nDo not remove this.\n\n"
            "<!-- mnemos-start -->\nmanaged block\n<!-- mnemos-end -->\n\n"
            "Keep this too.\n"
        )

        remove_claude_md_block(claude_md)
        content = claude_md.read_text()

        assert "Do not remove this." in content
        assert "Keep this too." in content
        assert "managed block" not in content

    def test_no_change_when_block_absent(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Some content\nno mnemos block here\n")

        changed, diff = remove_claude_md_block(claude_md)

        assert changed is False
        assert diff == ""

    def test_returns_false_for_missing_file(self, tmp_path):
        changed, diff = remove_claude_md_block(tmp_path / "CLAUDE.md")
        assert changed is False
        assert diff == ""

    def test_diff_contains_unified_diff_markers(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Header\n<!-- mnemos-start -->\nold\n<!-- mnemos-end -->\n"
        )
        _, diff = remove_claude_md_block(claude_md)
        assert "---" in diff
        assert "+++" in diff
        assert "-<!-- mnemos-start -->" in diff or "mnemos-start" in diff

    def test_idempotent_second_call(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Header\n\n<!-- mnemos-start -->\nblock\n<!-- mnemos-end -->\n"
        )

        remove_claude_md_block(claude_md)
        changed2, _ = remove_claude_md_block(claude_md)
        assert changed2 is False


# ---------------------------------------------------------------------------
# remove_cursor_rules_block
# ---------------------------------------------------------------------------

class TestRemoveCursorRulesBlock:
    def test_removes_block_from_rules_file(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text(
            "# Cursor rules\n\n"
            "<!-- mnemos:start -->\n## Memory\nuse mnemos search\n<!-- mnemos:end -->\n"
        )

        changed, diff = remove_cursor_rules_block(cursor_dir)

        assert changed is True
        content = rules.read_text()
        assert "<!-- mnemos:start -->" not in content
        assert "<!-- mnemos:end -->" not in content
        assert "mnemos search" not in content
        assert "# Cursor rules" in content

    def test_removes_block_from_rules_md_file(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules_md = cursor_dir / "rules.md"
        rules_md.write_text(
            "# Rules\n<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n"
        )

        changed, diff = remove_cursor_rules_block(cursor_dir)

        assert changed is True
        content = rules_md.read_text()
        assert "old" not in content
        assert "<!-- mnemos:start -->" not in content

    def test_prefers_rules_over_rules_md(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules_md = cursor_dir / "rules.md"
        rules.write_text("<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n")
        rules_md.write_text("# Should not be touched\n")

        remove_cursor_rules_block(cursor_dir)

        # rules.md must be unchanged
        assert rules_md.read_text() == "# Should not be touched\n"

    def test_no_change_when_block_absent(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text("no mnemos block here\n")

        changed, diff = remove_cursor_rules_block(cursor_dir)

        assert changed is False
        assert diff == ""

    def test_returns_false_when_cursor_dir_absent(self, tmp_path):
        cursor_dir = tmp_path / ".cursor_absent"
        changed, diff = remove_cursor_rules_block(cursor_dir)
        assert changed is False
        assert diff == ""

    def test_preserves_content_outside_block(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text(
            "# Keep me\n"
            "<!-- mnemos:start -->\nold\n<!-- mnemos:end -->\n"
            "# Keep me too\n"
        )

        remove_cursor_rules_block(cursor_dir)
        content = rules.read_text()

        assert "# Keep me" in content
        assert "# Keep me too" in content
        assert "old" not in content

    def test_idempotent_second_call(self, tmp_path):
        cursor_dir = tmp_path / ".cursor"
        cursor_dir.mkdir()
        rules = cursor_dir / "rules"
        rules.write_text("<!-- mnemos:start -->\nblock\n<!-- mnemos:end -->\n")

        remove_cursor_rules_block(cursor_dir)
        changed2, _ = remove_cursor_rules_block(cursor_dir)
        assert changed2 is False


# ---------------------------------------------------------------------------
# remove_zshrc_line
# ---------------------------------------------------------------------------

class TestRemoveZshrcLine:
    def test_removes_export_line(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text(
            'export PATH="$PATH:/usr/local/bin"\n'
            'export MNEMOS_REPO_ROOT="/home/user/mnemos"\n'
            'export EDITOR=vim\n'
        )

        changed, diff = remove_zshrc_line(zshrc)

        assert changed is True
        content = zshrc.read_text()
        assert "MNEMOS_REPO_ROOT" not in content
        assert 'export PATH="$PATH:/usr/local/bin"' in content
        assert "export EDITOR=vim" in content

    def test_removes_comment_line_above_export(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text(
            "# some unrelated comment\n"
            "# mnemos — set MNEMOS_REPO_ROOT\n"
            'export MNEMOS_REPO_ROOT="/home/user/mnemos"\n'
            "export EDITOR=vim\n"
        )

        changed, diff = remove_zshrc_line(zshrc)

        assert changed is True
        content = zshrc.read_text()
        assert "MNEMOS_REPO_ROOT" not in content
        assert "# mnemos" not in content
        assert "# some unrelated comment" in content
        assert "export EDITOR=vim" in content

    def test_no_change_when_line_absent(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text('export PATH="$PATH:/usr/local/bin"\n')

        changed, diff = remove_zshrc_line(zshrc)

        assert changed is False
        assert diff == ""

    def test_returns_false_for_missing_file(self, tmp_path):
        changed, diff = remove_zshrc_line(tmp_path / ".zshrc")
        assert changed is False
        assert diff == ""

    def test_diff_shows_removal(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text('export MNEMOS_REPO_ROOT="/repo"\n')
        _, diff = remove_zshrc_line(zshrc)
        assert "---" in diff
        assert "+++" in diff
        assert "MNEMOS_REPO_ROOT" in diff

    def test_idempotent_second_call(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text('export MNEMOS_REPO_ROOT="/repo"\n')

        remove_zshrc_line(zshrc)
        changed2, _ = remove_zshrc_line(zshrc)
        assert changed2 is False

    def test_handles_different_quote_styles(self, tmp_path):
        zshrc = tmp_path / ".zshrc"
        # No quotes around value
        zshrc.write_text("export MNEMOS_REPO_ROOT=/home/user/mnemos\n")

        changed, diff = remove_zshrc_line(zshrc)

        assert changed is True
        assert "MNEMOS_REPO_ROOT" not in zshrc.read_text()


# ---------------------------------------------------------------------------
# _read_pipx_metadata
# ---------------------------------------------------------------------------

class TestReadPipxMetadata:
    def test_reads_valid_metadata(self, tmp_path):
        venvs_dir = tmp_path / "pipx" / "venvs"
        mnemos_venv = venvs_dir / "mnemos"
        mnemos_venv.mkdir(parents=True)
        metadata = {"main_package": {"package": "mnemos", "package_version": "0.1.0"}}
        (mnemos_venv / "pipx_metadata.json").write_text(json.dumps(metadata))

        result = _read_pipx_metadata(venvs_dir)

        assert result == metadata

    def test_returns_none_when_file_absent(self, tmp_path):
        venvs_dir = tmp_path / "pipx" / "venvs"
        venvs_dir.mkdir(parents=True)

        result = _read_pipx_metadata(venvs_dir)

        assert result is None

    def test_returns_none_on_invalid_json(self, tmp_path):
        venvs_dir = tmp_path / "pipx" / "venvs"
        mnemos_venv = venvs_dir / "mnemos"
        mnemos_venv.mkdir(parents=True)
        (mnemos_venv / "pipx_metadata.json").write_text("not valid json {{{")

        result = _read_pipx_metadata(venvs_dir)

        assert result is None

    def test_returns_none_when_venvs_dir_absent(self, tmp_path):
        venvs_dir = tmp_path / "pipx" / "venvs_nonexistent"

        result = _read_pipx_metadata(venvs_dir)

        assert result is None


# ---------------------------------------------------------------------------
# purge_mnemos_artifacts
# ---------------------------------------------------------------------------

class TestPurgeMnemosArtifacts:
    def _make_dirs(self, tmp_path: Path):
        """Create fake bin_dir and venvs_dir with mnemos artifacts."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True)
        venvs_dir = tmp_path / "pipx" / "venvs"
        mnemos_venv = venvs_dir / "mnemos"
        mnemos_venv.mkdir(parents=True)

        # Fake binary
        binary = bin_dir / "mnemos"
        binary.write_text("#!/bin/sh\necho mnemos\n")

        # Fake metadata
        metadata = {"main_package": {"package": "mnemos"}}
        (mnemos_venv / "pipx_metadata.json").write_text(json.dumps(metadata))

        return bin_dir, venvs_dir

    def test_pipx_success_no_fallback_needed(self, tmp_path):
        """When pipx uninstall succeeds and removes artifacts, no fallback fires."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)
        binary = bin_dir / "mnemos"
        venv = venvs_dir / "mnemos"

        def fake_pipx_run(cmd, **kwargs):
            # Simulate pipx removing the binary and venv
            binary.unlink(missing_ok=True)
            shutil.rmtree(venv, ignore_errors=True)
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_pipx_run):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["pipx_ok"] is True
        # Fallback not needed because artifacts were already gone
        assert result["binary_removed"] is None
        assert result["venv_removed"] is None
        assert result["still_present"] is False

    def test_fallback_removes_binary_when_pipx_fails(self, tmp_path):
        """When pipx uninstall fails, binary and venv are removed by fallback."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "pipx")):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["pipx_ok"] is False
        assert result["binary_removed"] == bin_dir / "mnemos"
        assert result["venv_removed"] == venvs_dir / "mnemos"
        assert not (bin_dir / "mnemos").exists()
        assert not (venvs_dir / "mnemos").exists()
        assert result["still_present"] is False

    def test_fallback_removes_binary_when_pipx_not_found(self, tmp_path):
        """When pipx command is not found, fallback still cleans up artifacts."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)

        with patch("subprocess.run", side_effect=FileNotFoundError("pipx not found")):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["pipx_ok"] is False
        assert result["binary_removed"] == bin_dir / "mnemos"
        assert result["venv_removed"] == venvs_dir / "mnemos"

    def test_reports_still_present_when_which_finds_binary(self, tmp_path):
        """still_present is True when shutil.which finds the binary after cleanup."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "pipx")):
            with patch("shutil.which", return_value="/some/other/mnemos"):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["still_present"] is True

    def test_includes_metadata_in_result(self, tmp_path):
        """metadata key is populated when pipx_metadata.json exists."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "pipx")):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["metadata"] is not None
        assert result["metadata"]["main_package"]["package"] == "mnemos"

    def test_no_artifacts_nothing_removed(self, tmp_path):
        """When binary and venv are absent, fallback reports nothing removed."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        venvs_dir = tmp_path / "pipx" / "venvs"
        venvs_dir.mkdir(parents=True)

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "pipx")):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["binary_removed"] is None
        assert result["venv_removed"] is None
        assert result["metadata"] is None

    def test_pipx_success_but_artifacts_remain_triggers_fallback(self, tmp_path):
        """Even when pipx reports success but leaves artifacts, fallback cleans them up."""
        bin_dir, venvs_dir = self._make_dirs(tmp_path)

        # pipx succeeds but does NOT remove files
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            with patch("shutil.which", return_value=None):
                result = purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)

        assert result["pipx_ok"] is True
        # Fallback should still catch leftover artifacts
        assert result["binary_removed"] == bin_dir / "mnemos"
        assert result["venv_removed"] == venvs_dir / "mnemos"
        assert not (bin_dir / "mnemos").exists()
        assert not (venvs_dir / "mnemos").exists()


# ---------------------------------------------------------------------------
# run_uninstall (integration)
# ---------------------------------------------------------------------------

class TestRunUninstall:
    def _setup_home(self, tmp_path: Path) -> Path:
        """Create a fake home directory with all mnemos-managed content."""
        home = tmp_path / "home"
        claude_dir = home / ".claude"
        claude_dir.mkdir(parents=True)
        cursor_dir = home / ".cursor"
        cursor_dir.mkdir(parents=True)

        # settings.json
        settings = claude_dir / "settings.json"
        settings.write_text(
            json.dumps({
                "hooks": {
                    "PostToolUse": [
                        {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                    ],
                    "UserPromptSubmit": [
                        {"matcher": "", "hooks": [{"type": "command", "command": 'mnemos search "${CLAUDE_PROMPT:0:200}"'}]}
                    ],
                }
            }, indent=2) + "\n"
        )

        # CLAUDE.md
        claude_md = claude_dir / "CLAUDE.md"
        claude_md.write_text(
            "# Rules\n\n<!-- mnemos-start -->\nmanaged block\n<!-- mnemos-end -->\n"
        )

        # cursor rules
        rules = cursor_dir / "rules"
        rules.write_text("<!-- mnemos:start -->\nmanaged\n<!-- mnemos:end -->\n")

        # .zshrc
        zshrc = home / ".zshrc"
        zshrc.write_text('export MNEMOS_REPO_ROOT="/home/user/mnemos"\nexport EDITOR=vim\n')

        return home

    def test_removes_all_managed_content_with_yes(self, tmp_path):
        home = self._setup_home(tmp_path)

        exit_code = run_uninstall(yes=True, purge=False, home=home)

        assert exit_code == 0

        # settings.json — no mnemos hooks
        settings = home / ".claude" / "settings.json"
        result = json.loads(settings.read_text())
        assert "hooks" not in result

        # CLAUDE.md — no managed block
        claude_md = home / ".claude" / "CLAUDE.md"
        assert "<!-- mnemos-start -->" not in claude_md.read_text()

        # cursor rules — no managed block
        rules = home / ".cursor" / "rules"
        assert "<!-- mnemos:start -->" not in rules.read_text()

        # .zshrc — no MNEMOS_REPO_ROOT
        zshrc = home / ".zshrc"
        assert "MNEMOS_REPO_ROOT" not in zshrc.read_text()
        assert "EDITOR=vim" in zshrc.read_text()

    def test_nothing_to_uninstall_returns_zero(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text("{}\n")
        (home / ".claude" / "CLAUDE.md").write_text("# No mnemos block\n")

        exit_code = run_uninstall(yes=True, purge=False, home=home)

        assert exit_code == 0

    def test_aborted_on_no_confirmation(self, tmp_path):
        home = self._setup_home(tmp_path)

        with patch("builtins.input", return_value="n"):
            exit_code = run_uninstall(yes=False, purge=False, home=home)

        assert exit_code == 1
        # Files should be unchanged
        settings = home / ".claude" / "settings.json"
        result = json.loads(settings.read_text())
        assert "hooks" in result

    def test_proceeds_on_yes_confirmation(self, tmp_path):
        home = self._setup_home(tmp_path)

        with patch("builtins.input", return_value="y"):
            exit_code = run_uninstall(yes=False, purge=False, home=home)

        assert exit_code == 0
        settings = home / ".claude" / "settings.json"
        result = json.loads(settings.read_text())
        assert "hooks" not in result

    def test_purge_calls_purge_artifacts(self, tmp_path):
        home = self._setup_home(tmp_path)

        purge_result = {
            "pipx_ok": True,
            "binary_removed": None,
            "venv_removed": None,
            "metadata": None,
            "still_present": False,
        }
        with patch("core.uninstaller.purge_mnemos_artifacts", return_value=purge_result) as mock_purge:
            exit_code = run_uninstall(yes=True, purge=True, home=home)

        assert exit_code == 0
        mock_purge.assert_called_once()

    def test_purge_not_called_without_flag(self, tmp_path):
        home = self._setup_home(tmp_path)

        with patch("core.uninstaller.purge_mnemos_artifacts") as mock_purge:
            exit_code = run_uninstall(yes=True, purge=False, home=home)

        assert exit_code == 0
        mock_purge.assert_not_called()

    def test_purge_returns_one_when_binary_still_present(self, tmp_path):
        home = self._setup_home(tmp_path)

        purge_result = {
            "pipx_ok": False,
            "binary_removed": None,
            "venv_removed": None,
            "metadata": None,
            "still_present": True,  # binary still in PATH
        }
        with patch("core.uninstaller.purge_mnemos_artifacts", return_value=purge_result):
            exit_code = run_uninstall(yes=True, purge=True, home=home)

        assert exit_code == 1

    def test_eof_on_confirmation_returns_one(self, tmp_path):
        home = self._setup_home(tmp_path)

        with patch("builtins.input", side_effect=EOFError):
            exit_code = run_uninstall(yes=False, purge=False, home=home)

        assert exit_code == 1


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestUninstallCliCommand:
    def test_uninstall_command_exists(self):
        """uninstall sub-command must be registered on the CLI."""
        from core.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "uninstall" in result.output.lower()

    def test_uninstall_help_mentions_purge(self):
        from core.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert "--purge" in result.output

    def test_uninstall_help_mentions_yes(self):
        from core.cli import cli
        runner = CliRunner()
        result = runner.invoke(cli, ["uninstall", "--help"])
        assert "--yes" in result.output

    def test_cli_uninstall_with_yes_flag(self, tmp_path):
        """CLI --yes flag passes through to run_uninstall."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}\n")

        from core.cli import cli
        import core.uninstaller as uninstaller_module

        original = uninstaller_module.run_uninstall

        calls = []

        def fake_run_uninstall(yes=False, purge=False, home=None):
            calls.append({"yes": yes, "purge": purge})
            return 0

        uninstaller_module.run_uninstall = fake_run_uninstall
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["uninstall", "--yes"])
        finally:
            uninstaller_module.run_uninstall = original

        assert result.exit_code == 0
        assert len(calls) == 1
        assert calls[0]["yes"] is True
        assert calls[0]["purge"] is False

    def test_cli_uninstall_with_purge_flag(self, tmp_path):
        """CLI --purge flag passes through to run_uninstall."""
        from core.cli import cli
        import core.uninstaller as uninstaller_module

        original = uninstaller_module.run_uninstall
        calls = []

        def fake_run_uninstall(yes=False, purge=False, home=None):
            calls.append({"yes": yes, "purge": purge})
            return 0

        uninstaller_module.run_uninstall = fake_run_uninstall
        try:
            runner = CliRunner()
            result = runner.invoke(cli, ["uninstall", "--purge", "--yes"])
        finally:
            uninstaller_module.run_uninstall = original

        assert result.exit_code == 0
        assert calls[0]["purge"] is True
