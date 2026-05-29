"""Shared pytest fixtures for the mnemos test suite.

The single fixture here defends the suite against accidentally reading the
developer's real mnemos store. Several tests historically ran a live
``mnemos`` subprocess that honored whatever ``MNEMOS_REPO_ROOT`` happened to be
exported in the developer's shell (commonly ``~/.mnemos``). That made those
tests order/state-dependent: a real store with a pending promotion would make a
``<mnemos-context>`` assertion intermittently fail.

``isolate_mnemos_repo_root`` is ``autouse`` and *function-scoped*. pytest runs
autouse fixture setup before the test body, so a test that sets or deletes
``MNEMOS_REPO_ROOT`` itself (via its own ``monkeypatch``) still wins — its call
runs after this fixture's ``setenv`` and overrides it. Tests that
``delenv(..., raising=False)`` likewise override the default cleanly.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


# Capture the developer's REAL home ONCE at collection time, before any autouse
# fixture redirects HOME. ``isolate_home`` overrides HOME per test; this snapshot
# preserves the genuine home so ``real_home_snapshot`` can assert the live
# ``~/.claude`` is never touched by a test (Issue #70).
_REAL_HOME = Path.home()
_REAL_CLAUDE_EXISTED = (_REAL_HOME / ".claude").exists()


@pytest.fixture(autouse=True)
def isolate_home(tmp_path_factory, monkeypatch):
    """Redirect HOME / ``Path.home()`` / ``expanduser`` to a per-test temp dir.

    Companion to ``isolate_mnemos_repo_root`` (Issue #70). Three CLI paths
    resolve home directly — ``mnemos doctor`` (auto-repair), ``core.install``
    (setup), and ``core.updater`` (update) — and would otherwise detect the
    developer's real ``~/.claude`` and bake the per-test temp ``MNEMOS_REPO_ROOT``
    into the live ``settings.json``.

    This fixture is ``autouse`` and *function-scoped*. pytest runs autouse setup
    before the test body, so a per-test ``monkeypatch.setattr("core.cli.Path.home",
    ...)`` runs afterward and overrides it (the existing test_doctor.py home-patch
    tests keep passing). It does NOT replace ``isolate_mnemos_repo_root`` — both
    autouse fixtures run, so the existing MNEMOS_REPO_ROOT isolation stays intact.
    """
    isolated_home = tmp_path_factory.mktemp("home_isolated")

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(isolated_home)))


@pytest.fixture
def real_home_snapshot():
    """Expose the developer's real home captured before the HOME override.

    Returns ``(real_home: Path, real_claude_existed: bool)`` so a test can assert
    that running a home-resolving CLI with no per-test home patch never creates
    the live ``~/.claude`` directory.
    """
    return _REAL_HOME, _REAL_CLAUDE_EXISTED


@pytest.fixture
def safe_repo_root(monkeypatch):
    """Point MNEMOS_REPO_ROOT at a guard-SAFE repo_root.

    Issue #70: ``ClaudeCodeAdapter.install`` / ``mnemos doctor`` now refuse to
    template an *unsafe* MNEMOS_REPO_ROOT (empty, a temp/non-standard marker, or
    a path with no ``hooks/`` dir) into a real settings.json. The autouse
    ``isolate_mnemos_repo_root`` default is a ``pytest-of-`` temp dir — unsafe by
    design. Tests that assert hooks ARE templated must opt into this fixture,
    which supplies a marker-free path with an existing ``hooks/`` dir under the
    repository working tree and cleans it up afterward.
    """
    base = Path.cwd() / ".issue70-safe-repo-root"
    (base / "hooks").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(base))
    yield base

    import shutil

    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate_mnemos_repo_root(tmp_path_factory, monkeypatch):
    """Point MNEMOS_REPO_ROOT at a per-test temp dir.

    This is a defensive default only: it guarantees no test silently reads the
    developer's real store. Per-test ``monkeypatch.setenv`` / ``delenv`` calls
    run after this fixture and override it, so tests that manage the variable
    themselves are unaffected.

    The fixture deliberately does NOT touch ``MNEMOS_PROMO_CURSOR``: pinning the
    promotion cursor is a per-test concern (the search-output test sets it
    itself), and leaving it alone preserves the existing tests that exercise the
    default-cursor branch in ``core.context``.
    """
    isolated_root = tmp_path_factory.mktemp("mnemos_repo_root_isolated")
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(isolated_root))
