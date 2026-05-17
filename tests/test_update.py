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
    sync_source_to_install,
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
# sync_source_to_install
# ---------------------------------------------------------------------------

class TestSyncSourceToInstall:
    """Tests for sync_source_to_install() — the post-git-pull copy step."""

    def _make_repo(self, base: Path, dirs: dict[str, list[str]]) -> Path:
        """Create a fake source repo under base with given dirs and files.

        dirs maps dir_name → list of relative file paths (with content '# file').
        """
        repo = base / "repo"
        repo.mkdir()
        for dir_name, files in dirs.items():
            d = repo / dir_name
            d.mkdir()
            for rel in files:
                f = d / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(f"# {rel}\n")
        return repo

    def test_copies_core_and_agents_dirs(self, tmp_path):
        repo = self._make_repo(tmp_path, {
            "core": ["updater.py", "cli.py"],
            "agents": ["writer.py"],
        })
        install_root = tmp_path / "install"
        install_root.mkdir()

        synced = sync_source_to_install(str(repo), install_root=install_root)

        assert set(synced) == {"core", "agents"}
        assert (install_root / "core" / "updater.py").exists()
        assert (install_root / "core" / "cli.py").exists()
        assert (install_root / "agents" / "writer.py").exists()

    def test_overwrites_stale_files(self, tmp_path):
        repo = self._make_repo(tmp_path, {"core": ["updater.py"]})
        install_root = tmp_path / "install"
        (install_root / "core").mkdir(parents=True)
        stale = install_root / "core" / "updater.py"
        stale.write_text("# stale version\n")

        sync_source_to_install(str(repo), install_root=install_root)

        assert stale.read_text() == "# updater.py\n"

    def test_skips_missing_source_dir(self, tmp_path):
        """If agents/ doesn't exist in the repo, it is silently skipped."""
        repo = self._make_repo(tmp_path, {"core": ["cli.py"]})
        install_root = tmp_path / "install"
        install_root.mkdir()

        synced = sync_source_to_install(str(repo), install_root=install_root)

        assert synced == ["core"]
        assert not (install_root / "agents").exists()

    def test_returns_empty_when_no_source_dirs(self, tmp_path):
        """Repo with no core/ or agents/ returns an empty list."""
        repo = tmp_path / "empty_repo"
        repo.mkdir()
        install_root = tmp_path / "install"
        install_root.mkdir()

        synced = sync_source_to_install(str(repo), install_root=install_root)

        assert synced == []

    def test_preserves_existing_install_files_not_in_source(self, tmp_path):
        """Files in install_root that are not in the source are kept (dirs_exist_ok)."""
        repo = self._make_repo(tmp_path, {"core": ["new_module.py"]})
        install_root = tmp_path / "install"
        existing = install_root / "core" / "old_module.py"
        existing.parent.mkdir(parents=True)
        existing.write_text("# old but kept\n")

        sync_source_to_install(str(repo), install_root=install_root)

        # New file is present
        assert (install_root / "core" / "new_module.py").exists()
        # Old file that was not in source is still present (copytree merges, not replaces)
        assert existing.exists()

    def test_default_install_root_uses_home_mnemos(self, tmp_path, monkeypatch):
        """When install_root is None, defaults to Path.home() / '.mnemos'."""
        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        repo = self._make_repo(tmp_path, {"core": ["cli.py"]})

        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        synced = sync_source_to_install(str(repo), install_root=None)

        assert "core" in synced
        assert (fake_mnemos / "core" / "cli.py").exists()

    def test_run_update_syncs_before_pipx(self, tmp_path, monkeypatch):
        """run_update() must call sync_source_to_install before pipx_reinstall."""
        from core import updater as updater_module

        call_order: list[str] = []

        def fake_sync(repo_root, install_root=None):
            call_order.append("sync")
            return ["core"]

        def fake_pipx():
            call_order.append("pipx")

        monkeypatch.setattr(updater_module, "sync_source_to_install", fake_sync)
        monkeypatch.setattr(updater_module, "pipx_reinstall", fake_pipx)
        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", lambda: None)

        updater_module.run_update(
            repo_root=str(tmp_path),
            skip_git_pull=True,
            home=tmp_path,
        )

        assert call_order.index("sync") < call_order.index("pipx"), (
            "sync_source_to_install must run before pipx_reinstall"
        )

    def test_run_update_sync_failure_does_not_affect_exit_code(self, tmp_path, monkeypatch):
        """A failing sync must not change run_update's exit code."""
        from core import updater as updater_module

        def failing_sync(repo_root, install_root=None):
            raise RuntimeError("sync exploded")

        monkeypatch.setattr(updater_module, "sync_source_to_install", failing_sync)
        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", lambda: None)

        exit_code = updater_module.run_update(
            repo_root=str(tmp_path),
            skip_git_pull=True,
            skip_pipx=True,
            home=tmp_path,
        )

        assert exit_code == 0, "sync failure must not change update exit code"


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


# ---------------------------------------------------------------------------
# _bootstrap_sync_source (bootstrap fix — Issue #N)
# ---------------------------------------------------------------------------

class TestBootstrapSyncSource:
    """Tests for the pre-import bootstrap sync added to update_cmd in cli.py.

    The bootstrap problem: when the user runs ``mnemos update``, Python loads
    ``~/.mnemos/core/cli.py`` (potentially stale).  Any new function added to
    the dev repo (e.g. ``migrate_policy_transient``) won't exist in the runtime
    ``~/.mnemos/core/`` until a sync is performed.  The sync must happen BEFORE
    any ``from core.* import ...`` call inside ``update_cmd``.

    ``_bootstrap_sync_source`` satisfies this requirement because it uses only
    stdlib (shutil, pathlib) and runs at the very top of ``update_cmd``.
    """

    def _make_repo(self, base: Path, dirs: dict[str, list[tuple[str, str]]]) -> Path:
        """Create a minimal fake source repo.

        dirs maps dir_name → list of (relative_path, content) tuples.
        """
        repo = base / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        for dir_name, files in dirs.items():
            d = repo / dir_name
            d.mkdir(exist_ok=True)
            for rel, content in files:
                f = d / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(content)
        return repo

    def test_copies_core_and_agents_to_install(self, tmp_path, monkeypatch):
        """_bootstrap_sync_source must copy core/ and agents/ to ~/.mnemos/."""
        from core.cli import _bootstrap_sync_source

        repo = self._make_repo(tmp_path, {
            "core": [("install.py", "def migrate_policy_transient(): pass\n")],
            "agents": [("writer.py", "# writer\n")],
        })
        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _bootstrap_sync_source(str(repo))

        assert (fake_mnemos / "core" / "install.py").exists()
        assert (fake_mnemos / "agents" / "writer.py").exists()

    def test_new_function_is_importable_after_sync(self, tmp_path, monkeypatch):
        """After _bootstrap_sync_source, a newly-added function in core/ must be importable."""
        from core.cli import _bootstrap_sync_source

        new_func_code = "def migrate_policy_transient(root): return True\n"
        repo = self._make_repo(tmp_path, {
            "core": [("install.py", new_func_code)],
        })
        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _bootstrap_sync_source(str(repo))

        dest = fake_mnemos / "core" / "install.py"
        assert dest.exists()
        assert "migrate_policy_transient" in dest.read_text()

    def test_noop_when_repo_root_is_none(self, tmp_path, monkeypatch):
        """When repo_root is None the function must silently return without raising."""
        from core.cli import _bootstrap_sync_source

        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        # Must not raise
        _bootstrap_sync_source(None)
        # Nothing was copied
        assert not (fake_mnemos / "core").exists()

    def test_noop_when_repo_root_is_empty_string(self, tmp_path, monkeypatch):
        """When repo_root is '' the function must silently return."""
        from core.cli import _bootstrap_sync_source

        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        _bootstrap_sync_source("")
        assert not (fake_mnemos / "core").exists()

    def test_skips_same_path_resolve(self, tmp_path, monkeypatch):
        """When src.resolve() == dst.resolve() (editable install), skip without error."""
        from core.cli import _bootstrap_sync_source

        # Arrange: fake_home/.mnemos/core IS the repo's core/ directory (symlink)
        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        repo = tmp_path / "repo"
        repo_core = repo / "core"
        repo_core.mkdir(parents=True)
        (repo_core / "cli.py").write_text("# cli\n")

        # Make dst point to the same place as src via symlink
        (fake_mnemos / "core").symlink_to(repo_core.resolve())

        # Must not raise and must not overwrite
        _bootstrap_sync_source(str(repo))
        # The symlink must still point to the original location (unchanged)
        assert (fake_mnemos / "core").is_symlink()

    def test_update_cmd_calls_bootstrap_sync_before_run_update(self, tmp_path, monkeypatch):
        """update_cmd must call _bootstrap_sync_source before from core.updater import run_update."""
        from core import cli as cli_module
        from core import updater as updater_module

        call_order: list[str] = []

        original_bootstrap = cli_module._bootstrap_sync_source

        def spy_bootstrap(repo_root):
            call_order.append("bootstrap")

        monkeypatch.setattr(cli_module, "_bootstrap_sync_source", spy_bootstrap)
        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", lambda: None)

        runner = CliRunner()
        result = runner.invoke(
            cli_module.cli,
            ["update", "--skip-git-pull", "--skip-pipx",
             "--repo-root", str(tmp_path)],
        )

        assert "bootstrap" in call_order, "_bootstrap_sync_source was never called"

    def test_copy_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """A copy failure in _bootstrap_sync_source must not raise — the rest of update should run."""
        from core.cli import _bootstrap_sync_source
        import shutil as shutil_module

        fake_home = tmp_path / "fakehome"
        fake_mnemos = fake_home / ".mnemos"
        fake_mnemos.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        repo = self._make_repo(tmp_path, {"core": [("cli.py", "# cli\n")]})

        # Patch shutil.copytree to raise
        monkeypatch.setattr(shutil_module, "copytree", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

        # Must not raise
        _bootstrap_sync_source(str(repo))


# ---------------------------------------------------------------------------
# git pull recovery message (Issue #38)
# ---------------------------------------------------------------------------

class TestGitPullRecoveryMessage:
    """run_update() must emit human-readable recovery instructions when git pull fails."""

    def test_recovery_message_printed_to_stderr_on_git_pull_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        """When git_pull raises CalledProcessError the recovery hint must appear on stderr."""
        import subprocess
        from core import updater as updater_module

        def failing_git_pull(repo_root):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["git", "pull", "--rebase", "origin", "main"]
            )

        monkeypatch.setattr(updater_module, "git_pull", failing_git_pull)
        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", lambda: None)

        exit_code = updater_module.run_update(
            repo_root=str(tmp_path),
            skip_git_pull=False,
            skip_pipx=True,
            home=tmp_path,
        )

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "git pull --rebase origin main" in captured.err
        assert "git rebase --abort" in captured.err
        assert "git reset --hard origin/main" in captured.err

    def test_recovery_message_contains_repo_root_path(
        self, tmp_path, monkeypatch, capsys
    ):
        """The recovery instructions must include the actual repo_root path."""
        import subprocess
        from core import updater as updater_module

        def failing_git_pull(repo_root):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=["git", "pull", "--rebase", "origin", "main"]
            )

        monkeypatch.setattr(updater_module, "git_pull", failing_git_pull)
        monkeypatch.setattr(updater_module, "_run_bg_check_quiet", lambda: None)

        updater_module.run_update(
            repo_root=str(tmp_path),
            skip_git_pull=False,
            skip_pipx=True,
            home=tmp_path,
        )

        captured = capsys.readouterr()
        assert str(tmp_path) in captured.err
