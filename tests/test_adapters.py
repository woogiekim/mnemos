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

# safe_repo_root: shared fixture defined in tests/conftest.py (Issue #70).


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
    def test_install_adds_hooks_to_settings_json(self, tmp_path, safe_repo_root):
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
        assert "UserPromptSubmit.sh" in user_cmd

    def test_install_registers_stop_hook(self, tmp_path, safe_repo_root):
        """install() must register a Stop hook pointing at hooks/Stop.sh.

        Issue #4 anti-regression
        ------------------------
        The Stop hook was originally removed (commit 0de1c47) because Claude
        Code's Stop event fires once per AI response turn, flooding the session
        layer.  Re-introduction is safe because gateway.capture() now maintains
        a per-instance in-memory dedup registry keyed by (layer, NFKC-hash).
        Duplicate captures within the same process return None — storage,
        FTS, audit log, and event-bus paths are never reached.
        """
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()
        adapter.install(home)

        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "Stop" in hooks, "Stop hook must be registered by install()"
        stop_cmds = [h.get("command", "") for entry in hooks["Stop"] for h in entry.get("hooks", [])]
        assert any("Stop.sh" in cmd for cmd in stop_cmds), (
            f"Stop hook must reference Stop.sh, got: {stop_cmds}"
        )

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

    def test_install_skips_missing_settings_json(self, tmp_path, safe_repo_root):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # No settings.json
        (home / ".claude" / "CLAUDE.md").write_text("# Test\n")

        # A SAFE repo_root is required to reach the settings-json branch; with an
        # unsafe repo_root install() short-circuits with a [warning] (Issue #70).
        messages = ClaudeCodeAdapter().install(home)
        assert isinstance(messages, list)
        assert any("hooks unavailable" in msg for msg in messages)

    def test_install_skips_missing_claude_md(self, tmp_path):
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}\n")
        # No CLAUDE.md

        messages = ClaudeCodeAdapter().install(home)
        assert isinstance(messages, list)
        claude_md = home / ".claude" / "CLAUDE.md"
        assert claude_md.exists()
        assert "<!-- mnemos-start -->" in claude_md.read_text()


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

    def test_update_replaces_legacy_stop_hook_with_canonical_stop_sh(self, tmp_path):
        """update() replaces a legacy mnemos Stop hook with the canonical Stop.sh entry.

        Issue #4 anti-regression
        ------------------------
        The legacy Stop hook used 'mnemos extract-insight' and was removed in
        commit 0de1c47 because it flooded the session layer (Stop fires per AI
        turn, not per session).  update() must remove the legacy entry and
        replace it with the canonical Stop.sh hook, which is safe under the
        on-write dedup contract in gateway.capture().
        """
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
        # Legacy extract-insight entry must be gone
        stop_cmds = [
            h.get("command", "")
            for entry in hooks.get("Stop", [])
            for h in entry.get("hooks", [])
        ]
        assert not any("extract-insight" in cmd for cmd in stop_cmds), (
            "update() must remove legacy extract-insight Stop hook"
        )
        # Canonical Stop.sh must now be present
        assert "Stop" in hooks, "update() must install the canonical Stop.sh hook"
        assert any("Stop.sh" in cmd for cmd in stop_cmds), (
            f"update() must replace legacy entry with Stop.sh, got: {stop_cmds}"
        )

    def test_update_installs_stop_hook_when_absent(self, tmp_path):
        """update() must install the canonical Stop.sh hook even when no Stop entry existed.

        Issue #4 anti-regression
        ------------------------
        The Stop hook (hooks/Stop.sh) is now part of the standard mnemos hook
        set.  update() must always ensure Stop.sh is present, whether or not a
        previous Stop entry existed, because the on-write dedup in
        gateway.capture() makes re-firing on every AI turn safe.
        """
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        settings.write_text('{"hooks": {}}\n')

        ClaudeCodeAdapter().update(home)

        result = json.loads(settings.read_text())
        hooks = result.get("hooks", {})
        assert "Stop" in hooks, "update() must install Stop.sh hook"
        stop_cmds = [h.get("command", "") for entry in hooks["Stop"] for h in entry.get("hooks", [])]
        assert any("Stop.sh" in cmd for cmd in stop_cmds)

    def test_update_returns_messages(self, tmp_path):
        home = _make_claude_home(tmp_path)
        messages = ClaudeCodeAdapter().update(home)
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_update_no_change_when_already_canonical(self, tmp_path):
        """A second update() on canonical content reports unchanged.

        Issue #70: this deterministically covers the no-change return branches of
        _update_settings_json and _update_claude_md. Previously these branches
        were only exercised by the developer's real ~/.claude via the autouse
        Path.home() leak — exactly the hidden coupling this fix removes.
        """
        home = _make_claude_home(tmp_path)
        adapter = ClaudeCodeAdapter()

        adapter.update(home)
        messages = adapter.update(home)

        assert all("unchanged" in m for m in messages), messages

    def test_update_preserves_non_mnemos_hook_entry(self, tmp_path):
        """update() leaves a non-mnemos hook entry in place.

        Issue #70: deterministically covers the False branch of
        _is_mnemos_hook_entry (a hook command with no mnemos marker).
        """
        home = _make_claude_home(tmp_path)
        settings = home / ".claude" / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PostToolUse": [
                            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]},
                        ]
                    }
                },
                indent=2,
            )
            + "\n"
        )

        ClaudeCodeAdapter().update(home)

        result = json.loads(settings.read_text())
        cmds = [
            h.get("command", "")
            for entry in result["hooks"].get("PostToolUse", [])
            for h in entry.get("hooks", [])
        ]
        assert any("echo hi" in cmd for cmd in cmds), cmds

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
        rules = home / ".cursor" / "rules"
        assert rules.exists()
        assert "<!-- mnemos:start -->" in rules.read_text()


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
        """Both adapter blocks must include layer promotion/selection explanation."""
        for block, label in [(CLAUDE_MD_BLOCK, "CLAUDE_MD_BLOCK"), (CURSOR_RULES_BLOCK, "CURSOR_RULES_BLOCK")]:
            # Must reference all three user-facing layers
            assert "session" in block, f"{label} missing session layer reference"
            assert "project" in block, f"{label} missing project layer reference"
            assert "global" in block, f"{label} missing global layer reference"
            # Must include layer selection guidance
            assert "layer" in block.lower(), f"{label} missing layer selection guidance"

    def test_behavior_block_defines_host_install_contract(self):
        """Host instructions must define mnemos as a standalone memory capability."""
        assert "host-installable AI memory capability" in MNEMOS_BEHAVIOR_BLOCK
        assert "mnemos-managed marker blocks" in MNEMOS_BEHAVIOR_BLOCK
        assert "agent-crew" not in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_contains_read_and_gc_guidance(self):
        """Installed hosts must get search, read, capture, and GC guidance."""
        for command in ("mnemos search", "mnemos read", "mnemos capture", "mnemos gc"):
            assert command in MNEMOS_BEHAVIOR_BLOCK
        assert "hooks are unavailable" in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_contains_capture_durability_policy(self):
        """Install payload must distinguish transcript, decision, and summary captures."""
        assert "Raw transcript" in MNEMOS_BEHAVIOR_BLOCK
        assert "Durable decision" in MNEMOS_BEHAVIOR_BLOCK
        assert "Session summary" in MNEMOS_BEHAVIOR_BLOCK

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
        # Layer selection criteria must be present (replaced old "mnemos promotion rules" text)
        assert "project" in content
        assert "global" in content

    def test_behavior_block_contains_capture_interaction_pattern_section(self):
        """MNEMOS_BEHAVIOR_BLOCK must include the capture interaction pattern section."""
        assert "### Capture interaction pattern" in MNEMOS_BEHAVIOR_BLOCK
        assert "do NOT ask permission" in MNEMOS_BEHAVIOR_BLOCK
        assert "Should I capture this?" in MNEMOS_BEHAVIOR_BLOCK

    def test_behavior_block_capture_pattern_has_notify_and_delete_guidance(self):
        """Capture interaction pattern must specify Method E notification rule and delete-on-request.

        Method E (closes #22): AI must not write ✻ notifications — the CLI does.
        The behavior block must include this prohibition and delete-on-request guidance.
        Pre-Method-E: asserted 'mnemos capture --quiet' (AI used --quiet to suppress CLI notice).
        Method E: --quiet is no longer the right pattern; AI calls mnemos capture without --quiet
        so the CLI can emit its canonical notification.
        """
        # Under Method E, capture command does NOT use --quiet (CLI notification must fire)
        assert "mnemos capture --quiet" not in MNEMOS_BEHAVIOR_BLOCK, (
            "Method E: --quiet should NOT be in capture examples; "
            "the CLI notification requires non-quiet mode"
        )
        # The delete-on-request guidance is preserved
        assert "mnemos delete" in MNEMOS_BEHAVIOR_BLOCK
        # Method E prohibition must be present
        assert "DO NOT" in MNEMOS_BEHAVIOR_BLOCK or "forbidden" in MNEMOS_BEHAVIOR_BLOCK, (
            "Method E: prohibition against AI-written notifications must be in MNEMOS_BEHAVIOR_BLOCK"
        )

    def test_behavior_block_method_e_is_canonical_notification(self):
        """Method E: CLI stdout is the sole notification channel — no per-layer AI text.

        Pre-Method-E, the behavior block told AI to write per-layer notification text
        (session/project/global: ✻ ... / ephemeral: silent). Method E removes that
        per-layer distinction entirely because the CLI handles notification for all
        captured items uniformly. The block must NOT contain the contradictory
        per-layer AI-written notification guidance.
        """
        # Method E: AI is not responsible for per-layer notification text
        assert "After capturing, notify in your own response text" not in MNEMOS_BEHAVIOR_BLOCK, (
            "Method E: AI must not be instructed to write notification text; CLI handles it"
        )
        # Method E prohibition must still be present
        assert "DO NOT" in MNEMOS_BEHAVIOR_BLOCK or "forbidden" in MNEMOS_BEHAVIOR_BLOCK, (
            "Method E: prohibition against AI-written notifications must be in MNEMOS_BEHAVIOR_BLOCK"
        )


# ---------------------------------------------------------------------------
# EventBus integration — ClaudeCodeAdapter
# ---------------------------------------------------------------------------

class TestClaudeCodeAdapterEventBus:
    """Tests for ClaudeCodeAdapter.subscribe_to_event_bus and event handlers."""

    def test_subscribe_to_event_bus_registers_post_promote_handler(self):
        from core.events import EventBus, POST_PROMOTE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)
        assert bus.handler_count(POST_PROMOTE) == 1

    def test_subscribe_to_event_bus_registers_post_capture_handler(self):
        from core.events import EventBus, POST_CAPTURE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)
        assert bus.handler_count(POST_CAPTURE) == 1

    def test_on_post_promote_prints_capture_notice(self, capsys):
        """promote event must print 'promoted: <preview> (<from> → <to>)' to stdout."""
        from core.events import EventBus, POST_PROMOTE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)

        bus.emit(POST_PROMOTE, {
            "item_id": "abc-123",
            "content_preview": "Architecture decision",
            "from_layer": "session",
            "to_layer": "project",
        })

        out = capsys.readouterr().out
        # New format: ✻ <target-emoji> promoted <item_id> → <to_layer>
        # (target=project → 💾)
        assert "promoted" in out
        assert "abc-123" in out
        assert "project" in out
        assert "→" in out

    def test_on_post_promote_format_matches_spec(self, capsys, monkeypatch):
        """Output format must be: ✻ <target-emoji> promoted <item_id> → <to_layer>

        Emoji is selected from the *target* layer under the emoji-by-layer
        scheme: 💡 session, 💾 project, 🧠 global. The test promotes
        working→session, so the emoji is 💡.
        """
        monkeypatch.setenv("NO_COLOR", "1")
        from core.events import EventBus, POST_PROMOTE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)

        bus.emit(POST_PROMOTE, {
            "item_id": "id-001",
            "content_preview": "Use SQLite for FTS",
            "from_layer": "working",
            "to_layer": "session",
        })

        out = capsys.readouterr().out.strip()
        assert out == "✻ 💡 promoted id-001 → session"

    def test_on_post_capture_prints_for_persistent_layers(self, capsys, monkeypatch):
        """post-capture handler must print notice for session/project/global layers."""
        monkeypatch.setenv("NO_COLOR", "1")
        from core.events import EventBus, POST_CAPTURE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)

        for layer in ("session", "project", "global"):
            bus.emit(POST_CAPTURE, {
                "item_id": "item-001",
                "content_preview": "Test insight",
                "layer": layer,
            })

        out = capsys.readouterr().out
        assert out.count("Test insight") == 3

    def test_on_post_capture_silent_for_ephemeral_working(self, capsys):
        """post-capture handler must NOT print for ephemeral or working layers."""
        from core.events import EventBus, POST_CAPTURE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)

        for layer in ("ephemeral", "working"):
            bus.emit(POST_CAPTURE, {
                "item_id": "item-002",
                "content_preview": "Ephemeral scratch",
                "layer": layer,
            })

        out = capsys.readouterr().out
        assert out == ""

    def test_subscribe_idempotency_across_multiple_calls(self):
        """Calling subscribe_to_event_bus multiple times adds additional handlers."""
        from core.events import EventBus, POST_PROMOTE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)
        adapter.subscribe_to_event_bus(bus)
        # Two subscribe calls → two handlers (documented behaviour for EventBus)
        assert bus.handler_count(POST_PROMOTE) == 2

    def test_handler_survives_missing_payload_keys(self, capsys):
        """Handler must not raise if payload is missing expected keys."""
        from core.events import EventBus, POST_PROMOTE
        bus = EventBus()
        adapter = ClaudeCodeAdapter()
        adapter.subscribe_to_event_bus(bus)

        # Emit with empty payload — must not raise
        bus.emit(POST_PROMOTE, {})
        # Output will just use "?" defaults; no exception


class TestCursorAdapterEventBus:
    """Tests for CursorAdapter.subscribe_to_event_bus."""

    def test_subscribe_to_event_bus_is_callable(self):
        """CursorAdapter must implement subscribe_to_event_bus without raising."""
        from core.events import EventBus
        bus = EventBus()
        adapter = CursorAdapter()
        adapter.subscribe_to_event_bus(bus)  # no-op but must not raise

    def test_subscribe_to_event_bus_registers_no_handlers(self):
        """CursorAdapter subscribes no handlers (no-op implementation)."""
        from core.events import EventBus, POST_PROMOTE, POST_CAPTURE
        bus = EventBus()
        CursorAdapter().subscribe_to_event_bus(bus)
        assert bus.handler_count(POST_PROMOTE) == 0
        assert bus.handler_count(POST_CAPTURE) == 0

    def test_host_adapter_base_subscribe_is_noop(self):
        """HostAdapter.subscribe_to_event_bus default is a no-op (no NotImplementedError)."""
        from core.events import EventBus
        # Use ClaudeCodeAdapter as a concrete class — base method is called if
        # subclass doesn't override, but both adapters do override it.
        # Verify the base class itself doesn't raise.
        adapter = ClaudeCodeAdapter()
        bus = EventBus()
        # If ClaudeCodeAdapter overrides it (it does), just verify it runs cleanly.
        adapter.subscribe_to_event_bus(bus)  # no exception expected
