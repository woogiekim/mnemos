"""Adapter contract tests for core/adapters/.

Tests use tmp_path (pytest fixture) so no real home directory is touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.adapters import ClaudeCodeAdapter, CursorAdapter, HostAdapter
from core.adapters.base import MNEMOS_BEHAVIOR_BLOCK
from core.adapters.claude import CLAUDE_MD_BLOCK
from core.adapters.cursor import CURSOR_RULES_BLOCK


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_claude_home(tmp_path: Path) -> Path:
    """Create a fake home with ~/.claude directory and minimal files."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    # settings.json
    (claude_dir / "settings.json").write_text("{}\n")
    # CLAUDE.md
    (claude_dir / "CLAUDE.md").write_text("# Existing content\n")
    return home


def _make_cursor_home(tmp_path: Path) -> Path:
    """Create a fake home with ~/.cursor directory and a rules file."""
    home = tmp_path / "home"
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "rules").write_text("# Cursor rules\n")
    return home


# ---------------------------------------------------------------------------
# HostAdapter abstract interface
# ---------------------------------------------------------------------------

class TestHostAdapterInterface:
    def test_cannot_instantiate_abstract_base(self):
        with pytest.raises(TypeError):
            HostAdapter()  # type: ignore[abstract]

    def test_claude_adapter_is_host_adapter(self):
        assert isinstance(ClaudeCodeAdapter(), HostAdapter)

    def test_cursor_adapter_is_host_adapter(self):
        assert isinstance(CursorAdapter(), HostAdapter)


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.is_present
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterIsPresent:
    def test_true_when_dot_claude_exists(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        assert ClaudeCodeAdapter().is_present(home) is True

    def test_false_when_dot_claude_absent_and_no_binary(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("shutil.which", lambda _: None)
        assert ClaudeCodeAdapter().is_present(home) is False


# ---------------------------------------------------------------------------
# CursorAdapter.is_present
# ---------------------------------------------------------------------------

class TestCursorAdapterIsPresent:
    def test_true_when_dot_cursor_exists(self, tmp_path):
        home = tmp_path / "home"
        (home / ".cursor").mkdir(parents=True)
        assert CursorAdapter().is_present(home) is True

    def test_false_when_dot_cursor_absent(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        assert CursorAdapter().is_present(home) is False


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.install
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterInstall:
    def test_install_adds_hooks_to_settings_json(self, tmp_path):
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        messages = adapter.install(home)

        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "PostToolUse" in hooks
        assert "UserPromptSubmit" in hooks

        post_cmd = hooks["PostToolUse"][0]["hooks"][0]["command"]
        assert "mnemos ingest-claude-md" in post_cmd

        user_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        assert "mnemos search" in user_cmd

    def test_install_does_not_register_stop_hook(self, tmp_path):
        """install() must NOT register a Stop hook (fires per-response, not per-session)."""
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        adapter.install(home)

        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "Stop" not in hooks, "Stop hook must not be registered by install()"

    def test_install_writes_managed_block_to_claude_md(self, tmp_path):
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        adapter.install(home)

        claude_md = home / ".claude" / "CLAUDE.md"
        content = claude_md.read_text()
        assert "<!-- mnemos-start -->" in content
        assert "<!-- mnemos-end -->" in content
        assert "mnemos search" in content
        # Surrounding content preserved
        assert "# Existing content" in content

    def test_install_returns_messages(self, tmp_path):
        home = _make_claude_home(tmp_path)
        messages = ClaudeCodeAdapter().install(home)
        assert isinstance(messages, list)
        assert len(messages) > 0
        assert all(isinstance(m, str) for m in messages)

    def test_install_is_idempotent(self, tmp_path):
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        adapter.install(home)
        adapter.install(home)

        claude_md = home / ".claude" / "CLAUDE.md"
        content = claude_md.read_text()
        assert content.count("<!-- mnemos-start -->") == 1

    def test_install_skips_missing_settings_json(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # No settings.json
        (home / ".claude" / "CLAUDE.md").write_text("# Test\n")

        messages = ClaudeCodeAdapter().install(home)
        # Should not raise; no settings.json message expected
        assert isinstance(messages, list)

    def test_install_skips_missing_claude_md(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}\n")
        # No CLAUDE.md

        messages = ClaudeCodeAdapter().install(home)
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.update
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterUpdate:
    def test_update_replaces_old_block_in_claude_md(self, tmp_path):
        home = _make_claude_home(tmp_path)
        claude_md = home / ".claude" / "CLAUDE.md"
        claude_md.write_text(
            "# Header\n\n"
            "<!-- mnemos-start -->\n## Memory OLD\nold content\n<!-- mnemos-end -->\n"
        )

        adapter = ClaudeCodeAdapter()
        messages = adapter.update(home)

        content = claude_md.read_text()
        assert "Memory OLD" not in content
        assert "<!-- mnemos-start -->" in content
        assert "mnemos search" in content

    def test_update_replaces_hooks_in_settings_json(self, tmp_path):
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "OldMatcher",
                        "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/old" mnemos ingest-claude-md'}],
                    }
                ]
            }
        }
        settings.write_text(json.dumps(data, indent=2) + "\n")

        adapter = ClaudeCodeAdapter()
        messages = adapter.update(home)

        result = json.loads(settings.read_text())
        hooks = result["hooks"]["PostToolUse"]
        assert hooks[0]["matcher"] == "Write|Edit"

    def test_update_removes_legacy_stop_hook(self, tmp_path):
        """update() removes any previously-installed Stop hook (legacy or canonical)."""
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        data = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos extract-insight'}]},
                ],
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": 'MNEMOS_REPO_ROOT="/repo" mnemos ingest-claude-md'}]},
                ],
            }
        }
        settings.write_text(json.dumps(data, indent=2) + "\n")

        ClaudeCodeAdapter().update(home)

        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "Stop" not in hooks, "update() must remove any mnemos Stop hook"

    def test_update_does_not_add_stop_hook_when_absent(self, tmp_path):
        """update() must not add a Stop hook if none existed."""
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        settings.write_text('{"hooks": {}}\n')

        ClaudeCodeAdapter().update(home)

        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "Stop" not in hooks

    def test_update_returns_messages(self, tmp_path):
        home = _make_claude_home(tmp_path)
        messages = ClaudeCodeAdapter().update(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_update_returns_diff_in_messages_when_changed(self, tmp_path):
        home = _make_claude_home(tmp_path)
        claude_md = home / ".claude" / "CLAUDE.md"
        claude_md.write_text(
            "<!-- mnemos-start -->\nold content\n<!-- mnemos-end -->\n"
        )

        messages = ClaudeCodeAdapter().update(home)
        combined = "\n".join(messages)
        assert "---" in combined or "[updated]" in combined


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter.uninstall
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterUninstall:
    def test_uninstall_removes_hooks_from_settings_json(self, tmp_path):
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        data = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "mnemos ingest-claude-md"}]}
                ],
                "UserPromptSubmit": [
                    {"matcher": "", "hooks": [{"type": "command", "command": 'mnemos search "${CLAUDE_PROMPT:0:200}"'}]}
                ],
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "mnemos capture --layer session --content session-end --tag auto-stop"}]}
                ],
            }
        }
        settings.write_text(json.dumps(data, indent=2) + "\n")

        ClaudeCodeAdapter().uninstall(home)

        result = json.loads(settings.read_text())
        assert "hooks" not in result

    def test_uninstall_removes_legacy_stop_hook(self, tmp_path):
        """uninstall() removes a previously-installed Stop hook from settings.json."""
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        data = {
            "hooks": {
                "Stop": [
                    {"matcher": "", "hooks": [{"type": "command", "command": "mnemos capture --layer session --content session-end"}]}
                ]
            }
        }
        settings.write_text(json.dumps(data, indent=2) + "\n")

        ClaudeCodeAdapter().uninstall(home)

        result = json.loads(settings.read_text())
        assert "Stop" not in result.get("hooks", {})

    def test_uninstall_removes_managed_block_from_claude_md(self, tmp_path):
        home = _make_claude_home(tmp_path)
        claude_md = home / ".claude" / "CLAUDE.md"
        claude_md.write_text(
            "# My Rules\n\n"
            "<!-- mnemos-start -->\n## Memory\nmanaged\n<!-- mnemos-end -->\n\n"
            "Keep this.\n"
        )

        ClaudeCodeAdapter().uninstall(home)

        content = claude_md.read_text()
        assert "<!-- mnemos-start -->" not in content
        assert "<!-- mnemos-end -->" not in content
        assert "# My Rules" in content
        assert "Keep this." in content

    def test_uninstall_returns_messages(self, tmp_path):
        home = _make_claude_home(tmp_path)
        messages = ClaudeCodeAdapter().uninstall(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_uninstall_is_idempotent(self, tmp_path):
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        adapter.uninstall(home)
        messages = adapter.uninstall(home)
        assert all("unchanged" in m for m in messages)

    def test_uninstall_skips_missing_files(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # No settings.json, no CLAUDE.md
        messages = ClaudeCodeAdapter().uninstall(home)
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# CursorAdapter.install
# ---------------------------------------------------------------------------

class TestCursorAdapterInstall:
    def test_install_appends_block_to_rules(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        adapter = CursorAdapter()
        adapter.install(home)

        rules = home / ".cursor" / "rules"
        content = rules.read_text()
        assert "<!-- mnemos:start -->" in content
        assert "<!-- mnemos:end -->" in content
        assert "mnemos search" in content
        # Original content preserved
        assert "# Cursor rules" in content

    def test_install_returns_messages(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        messages = CursorAdapter().install(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_install_is_idempotent(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        adapter = CursorAdapter()
        adapter.install(home)
        adapter.install(home)

        rules = home / ".cursor" / "rules"
        content = rules.read_text()
        assert content.count("<!-- mnemos:start -->") == 1

    def test_install_skips_when_no_rules_file(self, tmp_path):
        home = tmp_path / "home"
        (home / ".cursor").mkdir(parents=True)
        # No rules file
        messages = CursorAdapter().install(home)
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# CursorAdapter.update
# ---------------------------------------------------------------------------

class TestCursorAdapterUpdate:
    def test_update_replaces_existing_block(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        rules = home / ".cursor" / "rules"
        rules.write_text(
            "# Rules\n\n"
            "<!-- mnemos:start -->\n## Memory OLD\nold\n<!-- mnemos:end -->\n"
        )

        adapter = CursorAdapter()
        messages = adapter.update(home)

        content = rules.read_text()
        assert "Memory OLD" not in content
        assert "<!-- mnemos:start -->" in content
        assert "mnemos search" in content
        assert "# Rules" in content

    def test_update_appends_when_block_absent(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        CursorAdapter().update(home)

        rules = home / ".cursor" / "rules"
        assert "<!-- mnemos:start -->" in rules.read_text()

    def test_update_no_change_when_already_canonical(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        rules = home / ".cursor" / "rules"
        rules.write_text(CURSOR_RULES_BLOCK + "\n")

        messages = CursorAdapter().update(home)
        assert all("unchanged" in m for m in messages)

    def test_update_returns_messages(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        messages = CursorAdapter().update(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_update_skips_when_no_cursor_dir(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        messages = CursorAdapter().update(home)
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# CursorAdapter.uninstall
# ---------------------------------------------------------------------------

class TestCursorAdapterUninstall:
    def test_uninstall_removes_block_from_rules(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        rules = home / ".cursor" / "rules"
        rules.write_text(
            "# Cursor rules\n\n"
            "<!-- mnemos:start -->\n## Memory\nmanaged\n<!-- mnemos:end -->\n\n"
            "Keep this.\n"
        )

        CursorAdapter().uninstall(home)

        content = rules.read_text()
        assert "<!-- mnemos:start -->" not in content
        assert "<!-- mnemos:end -->" not in content
        assert "# Cursor rules" in content
        assert "Keep this." in content

    def test_uninstall_returns_messages(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        messages = CursorAdapter().uninstall(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_uninstall_is_idempotent(self, tmp_path):
        home = _make_cursor_home(tmp_path)
        rules = home / ".cursor" / "rules"
        rules.write_text("<!-- mnemos:start -->\nblock\n<!-- mnemos:end -->\n")

        adapter = CursorAdapter()
        adapter.uninstall(home)
        messages = adapter.uninstall(home)
        assert all("unchanged" in m for m in messages)

    def test_uninstall_skips_when_no_rules_file(self, tmp_path):
        home = tmp_path / "home"
        (home / ".cursor").mkdir(parents=True)
        # No rules file
        messages = CursorAdapter().uninstall(home)
        assert isinstance(messages, list)

    def test_uninstall_returns_removed_label_when_cursor_dir_absent(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        # No .cursor dir at all
        messages = CursorAdapter().uninstall(home)
        assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# Adapter name property
# ---------------------------------------------------------------------------

class TestAdapterNames:
    def test_claude_adapter_name(self):
        assert ClaudeCodeAdapter().name == "Claude Code"

    def test_cursor_adapter_name(self):
        assert CursorAdapter().name == "Cursor"


# ---------------------------------------------------------------------------
# Behavioral block parity across adapters
# ---------------------------------------------------------------------------

class TestMnemosBehaviorBlockParity:
    """Verify that all adapter managed blocks share the canonical behavior text."""

    def test_claude_md_block_contains_behavior_block(self):
        """CLAUDE_MD_BLOCK must embed MNEMOS_BEHAVIOR_BLOCK verbatim."""
        assert MNEMOS_BEHAVIOR_BLOCK in CLAUDE_MD_BLOCK

    def test_cursor_rules_block_contains_behavior_block(self):
        """CURSOR_RULES_BLOCK must embed MNEMOS_BEHAVIOR_BLOCK verbatim."""
        assert MNEMOS_BEHAVIOR_BLOCK in CURSOR_RULES_BLOCK

    def test_both_blocks_contain_capture_rules(self):
        """Both adapter blocks must include capture instructions."""
        for block, label in [(CLAUDE_MD_BLOCK, "CLAUDE_MD_BLOCK"), (CURSOR_RULES_BLOCK, "CURSOR_RULES_BLOCK")]:
            assert "mnemos capture" in block, f"{label} missing capture instruction"
            assert "Stable project decisions" in block, f"{label} missing capture examples"
            assert "Do NOT capture" in block, f"{label} missing negative capture examples"

    def test_both_blocks_contain_search_rules(self):
        """Both adapter blocks must include search instructions."""
        for block, label in [(CLAUDE_MD_BLOCK, "CLAUDE_MD_BLOCK"), (CURSOR_RULES_BLOCK, "CURSOR_RULES_BLOCK")]:
            assert "mnemos search" in block, f"{label} missing search instruction"

    def test_both_blocks_contain_promotion_rules(self):
        """Both adapter blocks must include layer promotion explanation."""
        for block, label in [(CLAUDE_MD_BLOCK, "CLAUDE_MD_BLOCK"), (CURSOR_RULES_BLOCK, "CURSOR_RULES_BLOCK")]:
            assert "session layer" in block, f"{label} missing session layer reference"
            assert "mnemos promotion rules" in block, f"{label} missing promotion rules reference"

    def test_adapter_delimiters_are_preserved(self):
        """Adapter-specific HTML comment delimiters must be preserved."""
        assert CLAUDE_MD_BLOCK.startswith("<!-- mnemos-start -->")
        assert CLAUDE_MD_BLOCK.endswith("<!-- mnemos-end -->")
        assert CURSOR_RULES_BLOCK.startswith("<!-- mnemos:start -->")
        assert CURSOR_RULES_BLOCK.endswith("<!-- mnemos:end -->")

    def test_cursor_install_includes_full_capture_rules(self, tmp_path):
        """Cursor install must write the full behavioral block including capture rules."""
        home = _make_cursor_home(tmp_path)
        CursorAdapter().install(home)

        content = (home / ".cursor" / "rules").read_text()
        assert "mnemos capture" in content
        assert "Stable project decisions" in content
        assert "Do NOT capture" in content
        assert "mnemos promotion rules" in content

    def test_behavior_block_contains_capture_interaction_pattern_section(self):
        """MNEMOS_BEHAVIOR_BLOCK must include the capture interaction pattern section."""
        assert "### Capture interaction pattern" in MNEMOS_BEHAVIOR_BLOCK
        assert "do NOT ask permission" in MNEMOS_BEHAVIOR_BLOCK
        assert "Should I capture this?" in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_capture_pattern_has_notify_and_delete_guidance(self):
        """Capture interaction pattern must specify notify format and delete-on-request."""
        assert "mnemos capture --quiet" in MNEMOS_BEHAVIOR_BLOCK
        assert "(<layer>)" in MNEMOS_BEHAVIOR_BLOCK
        assert "mnemos delete" in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_notification_only_for_persistent_layers(self):
        """Notification must only apply to session/project/global; ephemeral/working are silent."""
        assert "session, project, global" in MNEMOS_BEHAVIOR_BLOCK
        assert "ephemeral, working" in MNEMOS_BEHAVIOR_BLOCK
        assert "no notification" in MNEMOS_BEHAVIOR_BLOCK
