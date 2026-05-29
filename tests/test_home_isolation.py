# Spec: prd.md § "Core feature list / input-output contract" — issue #70
"""Tests for the issue #70 test-isolation regression fix.

Three behaviors are proven here (derived from prd.md alone — the TDD red phase
runs before the implementer lands its source):

A. conftest companion autouse HOME isolation
   ``Path.home()`` / ``os.path.expanduser("~")`` / ``os.environ["HOME"]`` resolve
   to a per-test temp dir, NOT the developer's real home, while the existing
   ``MNEMOS_REPO_ROOT`` isolation stays intact. A per-test
   ``monkeypatch.setattr("core.cli.Path.home", ...)`` still wins over the
   autouse default. Driving ``mnemos doctor`` with NO home patch must NOT create
   a ``.claude`` directory under the developer's *real* home.

B. ``core.adapters.claude.is_unsafe_repo_root`` predicate + ``install()`` refusal
   The predicate flags empty/whitespace, temp/non-standard markers
   (``pytest-of-``, ``/T/``, ``/tmp/``, ``/private/var/folders``), and a missing
   ``{repo_root}/hooks`` directory. When the active ``MNEMOS_REPO_ROOT`` is
   unsafe, ``install()`` returns a ``[warning]`` message and leaves the
   settings.json hook commands free of the unsafe path. A safe repo_root (a
   non-temp-marker path with an existing ``hooks/`` dir) templates as before.

C. doctor hardening (core/cli.py)
   ``mnemos doctor`` auto-repair reuses the same guard: with an unsafe
   repo_root it does NOT bake the unsafe path into a real settings.json and
   does not exit non-zero for that condition alone; with a safe repo_root the
   existing ``FIXED`` repair still writes hooks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from core.adapters import ClaudeCodeAdapter
from core.adapters.claude import is_unsafe_repo_root
from core.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def safe_repo_root(tmp_path_factory):
    """A guard-SAFE repo_root: marker-free path with an existing ``hooks/`` dir.

    ``tmp_path`` lives under a ``pytest-of-`` / ``/private/var/folders`` marker,
    so a child of it is always flagged unsafe. To exercise the genuine SAFE
    branch of the *real* predicate we build the directory under the repository
    working tree (no temp marker in the path) and create a ``hooks/`` child so
    the on-disk existence check passes. The directory is cleaned up afterward.
    """
    base = Path.cwd() / ".issue70-safe-repo-root"
    hooks = base / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    yield base
    # best-effort cleanup
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def _make_claude_home(tmp_path: Path) -> Path:
    """Fake ~/.claude home with empty settings.json + CLAUDE.md."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}\n")
    (claude_dir / "CLAUDE.md").write_text("# Existing content\n")
    return home


def _make_claude_home_missing_hooks(tmp_path: Path) -> Path:
    """Fake home where Claude Code is detected but hooks are missing."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}\n")
    (claude_dir / "CLAUDE.md").write_text("# Some content without mnemos block\n")
    return home


def _all_hook_commands(settings_path: Path) -> list[str]:
    data = json.loads(settings_path.read_text())
    cmds: list[str] = []
    for _event, entries in data.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmds.append(hook.get("command", ""))
    return cmds


# ---------------------------------------------------------------------------
# A. conftest companion autouse HOME isolation
# ---------------------------------------------------------------------------

class TestHomeIsolationFixture:
    def test_path_home_resolves_to_temp_not_real_home(self):
        # given the autouse HOME-isolation fixture ran (no per-test home patch)
        # when home is resolved three different ways
        home_via_pathlib = str(Path.home())
        home_via_expanduser = os.path.expanduser("~")
        home_via_environ = os.environ.get("HOME", "")

        # then all three agree and point at the redirected temp HOME
        assert home_via_pathlib == home_via_environ
        assert os.path.realpath(home_via_expanduser) == os.path.realpath(home_via_environ)
        assert home_via_environ != ""
        # the redirected HOME is a temp dir (pytest tmp factory marker present)
        lowered = home_via_environ.lower()
        assert "pytest" in lowered or "tmp" in lowered or "var/folders" in lowered

    def test_mnemos_repo_root_isolation_still_intact(self):
        # given both autouse fixtures ran
        # then the existing MNEMOS_REPO_ROOT isolation is NOT weakened
        repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
        assert repo_root != ""
        assert "mnemos_repo_root_isolated" in repo_root

    def test_per_test_monkeypatch_home_overrides_fixture(self, tmp_path, monkeypatch):
        # given a per-test override of core.cli.Path.home
        custom_home = tmp_path / "custom_home"
        custom_home.mkdir()
        monkeypatch.setattr("core.cli.Path.home", lambda: custom_home)

        # when core.cli resolves home via the same symbol doctor_cmd uses
        from core.cli import Path as CliPath

        # then the per-test monkeypatch wins over the autouse fixture
        assert CliPath.home() == custom_home

    def test_doctor_with_no_home_patch_does_not_touch_real_home(
        self, runner, real_home_snapshot
    ):
        # given the developer's REAL home captured before the fixture override
        real_home, real_claude_existed = real_home_snapshot

        # when doctor runs with NO per-test home patch (relies on the fixture)
        result = runner.invoke(cli, ["doctor"])

        # then the run completes without error surfacing the real home
        assert result.exit_code in (0, 1), result.output
        # and the real ~/.claude is never created when it did not exist before
        real_claude = real_home / ".claude"
        if not real_claude_existed:
            assert not real_claude.exists(), (
                f"doctor created {real_claude} under the developer's real home"
            )


# ---------------------------------------------------------------------------
# B. is_unsafe_repo_root predicate
# ---------------------------------------------------------------------------

class TestIsUnsafeRepoRoot:
    def test_empty_string_is_unsafe(self):
        assert is_unsafe_repo_root("") is True

    def test_whitespace_only_is_unsafe(self):
        assert is_unsafe_repo_root("   ") is True

    def test_pytest_of_marker_is_unsafe(self):
        assert is_unsafe_repo_root(
            "/private/var/folders/xx/T/pytest-of-someone/pytest-1/repo0"
        ) is True

    def test_slash_T_marker_is_unsafe(self):
        assert is_unsafe_repo_root("/var/folders/zz/abc/T/whatever") is True

    def test_slash_tmp_marker_is_unsafe(self):
        assert is_unsafe_repo_root("/tmp/something/repo") is True

    def test_private_var_folders_marker_is_unsafe(self):
        assert is_unsafe_repo_root("/private/var/folders/aa/bb/repo") is True

    def test_marker_free_path_missing_hooks_dir_is_unsafe(self):
        # A marker-free path that does not exist on disk has no hooks/ child.
        assert is_unsafe_repo_root("/opt/definitely-not-a-real-repo-xyz-issue70") is True

    def test_marker_free_path_with_hooks_dir_is_safe(self, safe_repo_root):
        # A marker-free path WITH an existing hooks/ dir is SAFE.
        assert is_unsafe_repo_root(str(safe_repo_root)) is False


# ---------------------------------------------------------------------------
# B (cont). install() refuses unsafe repo_root; templates safe repo_root
# ---------------------------------------------------------------------------

class TestInstallRepoRootValidation:
    def test_install_refuses_unsafe_repo_root_and_warns(self, tmp_path):
        # given the active MNEMOS_REPO_ROOT is unsafe (the autouse temp default)
        unsafe_root = os.environ.get("MNEMOS_REPO_ROOT", "")
        assert is_unsafe_repo_root(unsafe_root) is True
        home = _make_claude_home(tmp_path)

        # when install runs
        sut = ClaudeCodeAdapter()
        messages = sut.install(home)

        # then a [warning] is returned and the unsafe path is NOT in the hooks
        assert any(m.startswith("[warning]") for m in messages), messages
        settings = home / ".claude" / "settings.json"
        cmds = _all_hook_commands(settings)
        assert all(unsafe_root not in cmd for cmd in cmds), cmds

    def test_install_templates_safe_repo_root(self, tmp_path, monkeypatch, safe_repo_root):
        # given a guard-SAFE repo_root (marker-free path + existing hooks/ dir)
        assert is_unsafe_repo_root(str(safe_repo_root)) is False
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(safe_repo_root))
        home = _make_claude_home(tmp_path)

        # when install runs with a safe repo_root
        sut = ClaudeCodeAdapter()
        sut.install(home)

        # then hooks are templated exactly as before
        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "PostToolUse" in hooks
        assert "UserPromptSubmit" in hooks

    def test_install_public_signature_returns_list_of_str(self, tmp_path):
        home = _make_claude_home(tmp_path)
        messages = ClaudeCodeAdapter().install(home)
        assert isinstance(messages, list)
        assert all(isinstance(m, str) for m in messages)


# ---------------------------------------------------------------------------
# C. doctor hardening — auto-repair refuses unsafe repo_root
# ---------------------------------------------------------------------------

class TestDoctorRepoRootHardening:
    def _run_doctor(self, runner, home: Path, monkeypatch):
        monkeypatch.setattr("core.cli.Path.home", lambda: home)
        return runner.invoke(cli, ["doctor"])

    def test_doctor_does_not_bake_unsafe_repo_root_into_settings(
        self, runner, tmp_path, monkeypatch
    ):
        # given the active MNEMOS_REPO_ROOT is unsafe (autouse temp default)
        unsafe_root = os.environ.get("MNEMOS_REPO_ROOT", "")
        assert is_unsafe_repo_root(unsafe_root) is True
        home = _make_claude_home_missing_hooks(tmp_path)

        # when doctor auto-repair runs
        result = self._run_doctor(runner, home, monkeypatch)

        # then it does NOT bake the unsafe path into settings.json hook commands
        settings = home / ".claude" / "settings.json"
        cmds = _all_hook_commands(settings)
        assert all(unsafe_root not in cmd for cmd in cmds), cmds
        # and the unsafe-repo_root condition alone does not make doctor exit non-zero
        assert result.exit_code == 0, result.output

    def test_doctor_safe_repo_root_still_fixes_missing_hooks(
        self, runner, tmp_path, monkeypatch, safe_repo_root
    ):
        # given a guard-SAFE repo_root
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(safe_repo_root))
        assert is_unsafe_repo_root(str(safe_repo_root)) is False
        home = _make_claude_home_missing_hooks(tmp_path)

        # when doctor auto-repair runs with a safe repo_root
        result = self._run_doctor(runner, home, monkeypatch)

        # then the legitimate repair still fires and writes the hooks
        assert result.exit_code == 0, result.output
        assert "FIXED" in result.output
        settings = home / ".claude" / "settings.json"
        data = json.loads(settings.read_text())
        hooks = data.get("hooks", {})
        assert "PostToolUse" in hooks
        assert "UserPromptSubmit" in hooks
