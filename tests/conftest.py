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

import pytest


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
