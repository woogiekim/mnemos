"""Tests for core/daemon.py — macOS launchd GC daemon management.

These tests mock launchctl and filesystem operations so they run on
any platform (including Linux CI).
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
from click.testing import CliRunner

from core.daemon import (
    PLIST_LABEL,
    PLIST_PATH,
    GC_LOG_DIR,
    GC_LOG_PATH,
    GC_ERROR_LOG_PATH,
    LAUNCH_AGENTS_DIR,
    _build_plist,
    _require_macos,
    install_gc_daemon,
    uninstall_gc_daemon,
    manage_gc_daemon,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_paths(tmp_path, monkeypatch):
    """Redirect plist and log paths to tmp_path so tests are side-effect-free."""
    fake_launch_agents = tmp_path / "LaunchAgents"
    fake_launch_agents.mkdir(parents=True)
    fake_log_dir = tmp_path / ".mnemos" / ".logs"
    fake_log_dir.mkdir(parents=True)
    fake_plist = fake_launch_agents / f"{PLIST_LABEL}.plist"

    monkeypatch.setattr("core.daemon.LAUNCH_AGENTS_DIR", fake_launch_agents)
    monkeypatch.setattr("core.daemon.PLIST_PATH", fake_plist)
    monkeypatch.setattr("core.daemon.GC_LOG_DIR", fake_log_dir)
    monkeypatch.setattr("core.daemon.GC_LOG_PATH", fake_log_dir / "gc.log")
    monkeypatch.setattr("core.daemon.GC_ERROR_LOG_PATH", fake_log_dir / "gc-error.log")

    return {
        "launch_agents": fake_launch_agents,
        "log_dir": fake_log_dir,
        "plist": fake_plist,
    }


def _fake_plist_path(tmp_path):
    """Return the redirected PLIST_PATH value after patching."""
    import core.daemon as _dm
    return _dm.PLIST_PATH


# ---------------------------------------------------------------------------
# _build_plist
# ---------------------------------------------------------------------------

class TestBuildPlist:
    def test_contains_label(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        assert PLIST_LABEL in xml

    def test_contains_bg_check_quiet(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        assert "bg-check" in xml
        assert "--quiet" in xml

    def test_contains_mnemos_bin(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        assert "/usr/local/bin/mnemos" in xml

    def test_schedule_is_3am(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        assert "<integer>3</integer>" in xml
        assert "<integer>0</integer>" in xml

    def test_contains_log_paths(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        assert "gc.log" in xml
        assert "gc-error.log" in xml

    def test_valid_xml_structure(self):
        xml = _build_plist("/usr/local/bin/mnemos")
        # Should start with XML declaration
        assert xml.startswith("<?xml")
        assert "<plist" in xml
        assert "</plist>" in xml


# ---------------------------------------------------------------------------
# _require_macos
# ---------------------------------------------------------------------------

class TestRequireMacos:
    def test_passes_on_darwin(self):
        with patch("platform.system", return_value="Darwin"):
            _require_macos()  # should not raise

    def test_raises_on_linux(self):
        with patch("platform.system", return_value="Linux"):
            with pytest.raises(RuntimeError, match="macOS"):
                _require_macos()

    def test_raises_on_windows(self):
        with patch("platform.system", return_value="Windows"):
            with pytest.raises(RuntimeError, match="macOS"):
                _require_macos()


# ---------------------------------------------------------------------------
# install_gc_daemon
# ---------------------------------------------------------------------------

class TestInstallGcDaemon:
    def test_writes_plist_file(self, tmp_path, monkeypatch):
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        with patch("shutil.which", return_value="/usr/local/bin/mnemos"), \
             patch("subprocess.run"):
            install_gc_daemon()

        plist = dm.PLIST_PATH
        assert plist.exists(), "plist file must be written"
        content = plist.read_text()
        assert PLIST_LABEL in content
        assert "bg-check" in content
        assert "--quiet" in content

    def test_calls_launchctl_load(self, tmp_path, monkeypatch):
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        with patch("shutil.which", return_value="/usr/local/bin/mnemos") as _which, \
             patch("subprocess.run") as mock_run:
            install_gc_daemon()

        # launchctl load -w <plist> must be called
        load_call = next(
            (c for c in mock_run.call_args_list
             if "launchctl" in str(c) and "load" in str(c)),
            None,
        )
        assert load_call is not None, "launchctl load must be invoked"
        assert str(dm.PLIST_PATH) in str(load_call)

    def test_creates_log_directory(self, tmp_path, monkeypatch):
        import core.daemon as dm
        # Remove the pre-created log dir to verify the function creates it
        import shutil
        shutil.rmtree(str(dm.GC_LOG_DIR), ignore_errors=True)

        monkeypatch.setattr("platform.system", lambda: "Darwin")
        with patch("shutil.which", return_value="/usr/local/bin/mnemos"), \
             patch("subprocess.run"):
            install_gc_daemon()

        assert dm.GC_LOG_DIR.exists(), "GC log dir must be created"

    def test_raises_on_non_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        with pytest.raises(RuntimeError, match="macOS"):
            install_gc_daemon()

    def test_raises_when_mnemos_not_on_path(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        with patch("shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="mnemos binary not found"):
                install_gc_daemon()

    def test_idempotent_reinstall_unloads_first(self, tmp_path, monkeypatch):
        """A second install should unload the old job before loading the new one."""
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        with patch("shutil.which", return_value="/usr/local/bin/mnemos"), \
             patch("subprocess.run") as mock_run:
            install_gc_daemon()
            install_gc_daemon()  # second call

        # At least one launchctl unload call must have been made
        unload_calls = [
            c for c in mock_run.call_args_list
            if "launchctl" in str(c) and "unload" in str(c)
        ]
        assert len(unload_calls) >= 1


# ---------------------------------------------------------------------------
# uninstall_gc_daemon
# ---------------------------------------------------------------------------

class TestUninstallGcDaemon:
    def test_removes_plist_file(self, tmp_path, monkeypatch):
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")

        # Pre-write a plist so uninstall has something to remove
        dm.PLIST_PATH.write_text("<plist/>")

        with patch("subprocess.run"):
            uninstall_gc_daemon()

        assert not dm.PLIST_PATH.exists(), "plist must be removed"

    def test_calls_launchctl_unload(self, tmp_path, monkeypatch):
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        dm.PLIST_PATH.write_text("<plist/>")

        with patch("subprocess.run") as mock_run:
            uninstall_gc_daemon()

        unload_calls = [
            c for c in mock_run.call_args_list
            if "launchctl" in str(c) and "unload" in str(c)
        ]
        assert len(unload_calls) == 1

    def test_raises_when_plist_missing(self, tmp_path, monkeypatch):
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        # Ensure plist does NOT exist
        dm.PLIST_PATH.unlink(missing_ok=True)

        with pytest.raises(FileNotFoundError, match="not found"):
            uninstall_gc_daemon()

    def test_raises_on_non_macos(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        with pytest.raises(RuntimeError, match="macOS"):
            uninstall_gc_daemon()

    def test_non_fatal_launchctl_unload_failure(self, tmp_path, monkeypatch):
        """If launchctl unload fails (job not loaded), removal must still proceed."""
        import core.daemon as dm
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        dm.PLIST_PATH.write_text("<plist/>")

        def _fail_unload(cmd, **kw):
            if "unload" in cmd:
                raise subprocess.CalledProcessError(1, cmd)

        with patch("subprocess.run", side_effect=_fail_unload):
            # Should not raise; plist should still be removed after warning
            uninstall_gc_daemon()

        assert not dm.PLIST_PATH.exists(), "plist must be removed even if launchctl unload fails"


# ---------------------------------------------------------------------------
# manage_gc_daemon
# ---------------------------------------------------------------------------

class TestManageGcDaemon:
    def test_install_flag_calls_install(self, monkeypatch):
        with patch("core.daemon.install_gc_daemon") as mock_install, \
             patch("core.daemon.uninstall_gc_daemon") as mock_uninstall:
            manage_gc_daemon(install=True, uninstall=False)
        mock_install.assert_called_once()
        mock_uninstall.assert_not_called()

    def test_uninstall_flag_calls_uninstall(self, monkeypatch):
        with patch("core.daemon.install_gc_daemon") as mock_install, \
             patch("core.daemon.uninstall_gc_daemon") as mock_uninstall:
            manage_gc_daemon(install=False, uninstall=True)
        mock_uninstall.assert_called_once()
        mock_install.assert_not_called()

    def test_both_flags_exits_with_error(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            manage_gc_daemon(install=True, uninstall=True)
        assert exc_info.value.code == 1

    def test_runtime_error_exits_with_error(self, monkeypatch):
        with patch("core.daemon.install_gc_daemon", side_effect=RuntimeError("not macOS")):
            with pytest.raises(SystemExit) as exc_info:
                manage_gc_daemon(install=True, uninstall=False)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI integration: mnemos gc --install-daemon / --uninstall-daemon
# ---------------------------------------------------------------------------

class TestGcDaemonCli:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_install_daemon_flag_is_present(self, runner):
        from core.cli import cli
        result = runner.invoke(cli, ["gc", "--help"])
        assert result.exit_code == 0
        assert "--install-daemon" in result.output

    def test_uninstall_daemon_flag_is_present(self, runner):
        from core.cli import cli
        result = runner.invoke(cli, ["gc", "--help"])
        assert result.exit_code == 0
        assert "--uninstall-daemon" in result.output

    def test_install_daemon_invokes_manage(self, runner, monkeypatch):
        """--install-daemon must call manage_gc_daemon(install=True)."""
        called_with = {}

        def fake_manage(install, uninstall):
            called_with["install"] = install
            called_with["uninstall"] = uninstall

        monkeypatch.setattr("core.daemon.manage_gc_daemon", fake_manage)
        from core.cli import cli
        result = runner.invoke(cli, ["gc", "--install-daemon"])
        assert called_with.get("install") is True
        assert called_with.get("uninstall") is False

    def test_uninstall_daemon_invokes_manage(self, runner, monkeypatch):
        """--uninstall-daemon must call manage_gc_daemon(uninstall=True)."""
        called_with = {}

        def fake_manage(install, uninstall):
            called_with["install"] = install
            called_with["uninstall"] = uninstall

        monkeypatch.setattr("core.daemon.manage_gc_daemon", fake_manage)
        from core.cli import cli
        result = runner.invoke(cli, ["gc", "--uninstall-daemon"])
        assert called_with.get("install") is False
        assert called_with.get("uninstall") is True


# ---------------------------------------------------------------------------
# CLI: mnemos bg-check --quiet
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal mnemos repo structure under tmp_path."""
    import yaml
    wiki = tmp_path / "wiki"
    for d in ["global", "projects"]:
        (wiki / d).mkdir(parents=True)
    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state"]:
        (agent / d).mkdir(parents=True)
    policy = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


class TestBgCheckQuiet:
    @pytest.fixture
    def runner(self):
        return CliRunner()

    @pytest.fixture
    def repo_root(self, tmp_path):
        return _make_repo(tmp_path)

    def test_quiet_flag_is_present(self, runner):
        from core.cli import cli
        result = runner.invoke(cli, ["bg-check", "--help"])
        assert result.exit_code == 0
        assert "--quiet" in result.output

    def test_quiet_suppresses_all_output(self, runner, repo_root, monkeypatch):
        """--quiet must produce no stdout even when there is activity."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))

        # Mock run_background_check to return an active result
        from core.bg import BackgroundCheckResult
        mock_result = BackgroundCheckResult(
            ran=True,
            gc_archived=5,
            promoted=2,
        )

        with patch("core.bg.run_background_check", return_value=mock_result):
            from core.cli import cli
            result = runner.invoke(cli, ["bg-check", "--quiet"])

        assert result.exit_code == 0, result.output
        assert result.output == "", f"Expected empty output, got: {result.output!r}"

    def test_without_quiet_shows_activity_block(self, runner, repo_root, monkeypatch):
        """Without --quiet, activity context block must appear."""
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))

        from core.bg import BackgroundCheckResult
        mock_result = BackgroundCheckResult(
            ran=True,
            gc_archived=3,
            promoted=0,
        )

        with patch("core.bg.run_background_check", return_value=mock_result):
            from core.cli import cli
            result = runner.invoke(cli, ["bg-check"])

        assert result.exit_code == 0, result.output
        assert "mnemos-context" in result.output or "GC archived" in result.output
