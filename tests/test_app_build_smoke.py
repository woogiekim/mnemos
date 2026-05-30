"""Heavy build-smoke test for the macOS .app bundle (issue #94).

This test runs ``pyinstaller --noconfirm app/mnemos_app.spec`` in an isolated
tempdir and asserts the assembled ``dist/mnemos.app`` carries the load-bearing
files (``Contents/Info.plist``, ``Contents/MacOS/mnemos``,
``Contents/Resources/core/templates/ui.html``).

The full PyInstaller run takes ~30-90 seconds and writes ~200MB to ``dist/``
and ``build/``, so the test is GATED behind the ``@pytest.mark.app_build``
marker and SKIPPED by default. Opt in with::

    pytest -m app_build

The marker is registered in ``pyproject.toml`` under
``[tool.pytest.ini_options] markers`` so the default suite continues to run
clean under ``filterwarnings=["error"]``. If pyinstaller is not installed in
the active environment the test is skipped (no install side effect).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.app_build
def test_pyinstaller_builds_macos_app_bundle(tmp_path):
    """Run pyinstaller against the spec and verify the assembled .app bundle."""
    # Skip cleanly when pyinstaller isn't available rather than failing —
    # the marker is opt-in, but we still want a friendly skip on machines
    # that opted in without first installing the build extra.
    pytest.importorskip("PyInstaller")

    spec_src = REPO_ROOT / "app" / "mnemos_app.spec"
    assert spec_src.exists(), f"missing spec: {spec_src}"

    # Stage the build inside tmp_path so we never pollute the repo's dist/
    # or build/ directories. The spec uses pathex=["."] and ../core/templates
    # data paths, so we mirror the layout the spec expects.
    work = tmp_path / "build-root"
    work.mkdir()
    # Copy the app/ and core/ directories (the spec's only inputs).
    shutil.copytree(REPO_ROOT / "app", work / "app")
    shutil.copytree(REPO_ROOT / "core", work / "core")

    # Run pyinstaller from the staged work dir so dist/ + build/ land here.
    spec = work / "app" / "mnemos_app.spec"
    env = os.environ.copy()
    # Ensure the subprocess sees the staged tree on PYTHONPATH so core.* imports
    # resolve to the copied modules.
    env["PYTHONPATH"] = str(work) + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        ["pyinstaller", "--noconfirm", str(spec)],
        cwd=str(work),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        f"pyinstaller failed (exit={result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )

    app_path = work / "dist" / "mnemos.app"
    assert app_path.is_dir(), f"missing .app bundle at {app_path}"

    info_plist = app_path / "Contents" / "Info.plist"
    binary = app_path / "Contents" / "MacOS" / "mnemos"
    ui_template = (
        app_path / "Contents" / "Resources" / "core" / "templates" / "ui.html"
    )

    assert info_plist.is_file(), f"missing Info.plist at {info_plist}"
    assert binary.is_file(), f"missing entry binary at {binary}"
    assert ui_template.is_file(), f"missing ui template at {ui_template}"

    # Sanity: binary is executable.
    assert os.access(binary, os.X_OK), f"entry binary not executable: {binary}"
