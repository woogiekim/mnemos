"""Unit tests for the macOS .app entry script (issue #94).

The desktop app entry (``app/mnemos_app.py``) is the alternative entry point to
the unified inspection UI delivered in #83. It MUST:

* set ``MNEMOS_REPO_ROOT`` to ``~/.mnemos`` only when the env var is unset (so
  Finder-launched bundles, which do NOT inherit shell env, agree with the
  ``mnemos`` CLI's default install root);
* respect an existing ``MNEMOS_REPO_ROOT`` instead of overwriting it;
* short-circuit to a headless ``write_unified_html`` + ``sys.exit(0)`` path
  when ``MNEMOS_APP_HEADLESS=1`` is set, NEVER importing ``webview``;
* otherwise build the payload, render the HTML, and hand it to
  :func:`core.unifiedview.launch_app` with ``title="mnemos"``.

These tests cover each branch end-to-end via the ``sys.modules['webview']``
injection pattern from #83 (mirroring ``tests/test_unifiedview.py:316``-style
fakes) so no real pywebview window opens. The module is reloaded per-test
because ``main()`` has side effects on ``os.environ`` that we want isolated.
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml


# --------------------------------------------------------------------------- #
# Repo-root fixture — mirrors the test_cli_ui.py policy/wiki shape so the
# gateway bootstraps cleanly without crashing on a missing policy.yaml.
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo_root(tmp_path):
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy_cfg = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
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
                "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy_cfg))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


def _fake_webview(captured: dict) -> types.ModuleType:
    """Return a fake ``webview`` module that captures the launch invocation."""
    fake = types.ModuleType("webview")

    def create_window(*args, **kwargs):
        captured["create_window_args"] = args
        captured["create_window_kwargs"] = kwargs
        # mirror the production call shape: create_window(title, url=...)
        captured["title"] = args[0] if args else kwargs.get("title")
        captured["url"] = kwargs.get("url", args[1] if len(args) > 1 else None)

    def start():
        captured["started"] = True

    fake.create_window = MagicMock(side_effect=create_window)
    fake.start = MagicMock(side_effect=start)
    return fake


def _reload_app_module():
    """Force-reload app.mnemos_app so each test exercises a fresh import.

    ``main()`` mutates ``os.environ`` and re-imports core modules; reloading
    keeps each test isolated even when an earlier test populated a different
    env state.
    """
    if "app.mnemos_app" in sys.modules:
        del sys.modules["app.mnemos_app"]
    if "app" in sys.modules:
        del sys.modules["app"]
    import app.mnemos_app  # noqa: F401  # imported for side effect (registration)
    return importlib.import_module("app.mnemos_app")


# --------------------------------------------------------------------------- #
# Case 1 — MNEMOS_REPO_ROOT unset → defaults to ~/.mnemos BEFORE core import
# --------------------------------------------------------------------------- #
class TestRepoRootDefault:
    def test_default_repo_root_when_env_unset(self, repo_root, monkeypatch, tmp_path):
        """Unset MNEMOS_REPO_ROOT → entry sets it to expanduser(~/.mnemos)."""
        # Stage a usable ~/.mnemos that the gateway can bootstrap against.
        # ``isolate_home`` (conftest.py) already redirects Path.home() into a
        # per-test temp dir; we mirror the same policy/wiki shape there so
        # the gateway doesn't crash on a missing policy.yaml.
        home_mnemos = Path(os.path.expanduser("~/.mnemos"))
        # Reuse the staged repo_root by symlink-equivalent copy of just the
        # files the gateway needs.
        home_mnemos.mkdir(parents=True, exist_ok=True)
        (home_mnemos / "wiki").mkdir(parents=True, exist_ok=True)
        for d in ["global", "projects", "entities", "claims", "topics"]:
            (home_mnemos / "wiki" / d).mkdir(parents=True, exist_ok=True)
        (home_mnemos / "wiki" / "policy.yaml").write_text(
            (repo_root / "wiki" / "policy.yaml").read_text()
        )
        (home_mnemos / "wiki" / "log.md").write_text("# Log\n")
        (home_mnemos / "wiki" / "log.jsonl").write_text("")

        monkeypatch.delenv("MNEMOS_REPO_ROOT", raising=False)
        monkeypatch.setenv("MNEMOS_APP_HEADLESS", "1")
        out_path = tmp_path / "headless-default.html"
        monkeypatch.setenv("MNEMOS_APP_HEADLESS_OUTPUT", str(out_path))

        # webview must not be imported on the headless path.
        sys.modules.pop("webview", None)

        mod = _reload_app_module()
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0

        # The entry's first action is to set MNEMOS_REPO_ROOT — verify it was
        # set to the expanded home path.
        assert os.environ["MNEMOS_REPO_ROOT"] == os.path.expanduser("~/.mnemos")
        # Headless path produced the file.
        assert out_path.exists()
        assert out_path.stat().st_size > 0
        # And it did NOT import webview.
        assert "webview" not in sys.modules


# --------------------------------------------------------------------------- #
# Case 2 — MNEMOS_REPO_ROOT pre-set → entry must NOT overwrite it
# --------------------------------------------------------------------------- #
class TestRepoRootRespectsEnv:
    def test_existing_repo_root_is_preserved(self, repo_root, monkeypatch, tmp_path):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        monkeypatch.setenv("MNEMOS_APP_HEADLESS", "1")
        out_path = tmp_path / "headless-env.html"
        monkeypatch.setenv("MNEMOS_APP_HEADLESS_OUTPUT", str(out_path))

        sys.modules.pop("webview", None)

        mod = _reload_app_module()
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        # The pre-set value must be preserved verbatim.
        assert os.environ["MNEMOS_REPO_ROOT"] == str(repo_root)
        assert out_path.exists()
        assert "webview" not in sys.modules


# --------------------------------------------------------------------------- #
# Case 3 — MNEMOS_APP_HEADLESS=1 → writes HTML, exits 0, no webview import
# --------------------------------------------------------------------------- #
class TestHeadlessMode:
    def test_headless_mode_writes_html_and_exits_zero(
        self, repo_root, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        monkeypatch.setenv("MNEMOS_APP_HEADLESS", "1")
        out_path = tmp_path / "headless.html"
        monkeypatch.setenv("MNEMOS_APP_HEADLESS_OUTPUT", str(out_path))

        # Pre-populate sys.modules with None so any accidental ``import webview``
        # raises ImportError — proves the headless path never imports it.
        sys.modules.pop("webview", None)
        monkeypatch.setitem(sys.modules, "webview", None)

        mod = _reload_app_module()
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        assert out_path.exists()
        text = out_path.read_text(encoding="utf-8")
        assert 'id="ui-data"' in text


# --------------------------------------------------------------------------- #
# Case 4 — Default GUI path → launch_app is called via injected fake webview
# --------------------------------------------------------------------------- #
class TestLaunchPath:
    def test_default_path_calls_launch_app_with_mnemos_title(
        self, repo_root, monkeypatch
    ):
        monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo_root))
        # Explicitly unset the headless flag so the default branch runs.
        monkeypatch.delenv("MNEMOS_APP_HEADLESS", raising=False)

        captured: dict = {}
        fake = _fake_webview(captured)
        monkeypatch.setitem(sys.modules, "webview", fake)

        mod = _reload_app_module()
        # The default path does NOT call sys.exit — it returns normally after
        # launch_app's blocking start() (here a no-op) returns.
        mod.main()

        # create_window was called with title=="mnemos" and a file:// URL
        # (the #83 launcher uses a temp-file file:// load, not inline html=).
        assert captured.get("title") == "mnemos"
        url = captured.get("url") or ""
        assert url.startswith("file://"), f"expected file:// URL, got {url!r}"
        # start() was invoked (the blocking event loop).
        assert captured.get("started") is True
        # And no inline html= was passed.
        assert "html" not in captured["create_window_kwargs"]
