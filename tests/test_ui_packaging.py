"""Wheel-build regression test for ``mnemos ui`` packaging (issue #83).

Mirrors :mod:`tests.test_inspect_packaging` (issue #80). The new
``core/templates/ui.html`` MUST ship in the built wheel via the existing
``[tool.setuptools.package-data] core = ["templates/*.html"]`` glob, and the
in-venv ``mnemos ui --output`` command MUST be able to load it via
``importlib.resources``.

Additionally — unique to #83 — this test asserts the OPTIONAL ``[ui]`` extra is
declared in the built wheel's metadata (``Provides-Extra: ui`` +
``Requires-Dist: pywebview...; extra == "ui"``), proving the extra ships
WITHOUT installing pywebview into any venv.

Runtime: ~10-30s (wheel build + venv create + pip install). No opt-in mark.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Fixtures / constants
# --------------------------------------------------------------------------- #
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_REL_PATH = "core/templates/ui.html"
SAMPLE_REPO_ROOT = PROJECT_ROOT / "repo"


def _build_env() -> dict[str, str]:
    """Return an env where build subprocesses can see pytest's site packages."""
    env = os.environ.copy()
    import_paths = [p for p in sys.path if p and "site-packages" in p]
    existing = env.get("PYTHONPATH", "")
    if existing:
        import_paths.append(existing)
    if import_paths:
        env["PYTHONPATH"] = os.pathsep.join(import_paths)
    return env


def _build_wheel(tmp_path: Path) -> Path:
    """Build a wheel from PROJECT_ROOT into tmp_path; return the .whl path."""
    outdir = tmp_path / "dist"
    outdir.mkdir()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(outdir),
        ],
        check=True,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=_build_env(),
    )

    wheels = sorted(outdir.glob("*.whl"))
    assert wheels, (
        f"No wheel produced under {outdir}. Files: {list(outdir.iterdir())}"
    )
    assert len(wheels) == 1, f"Expected exactly one wheel, got {wheels}"
    return wheels[0]


def _create_venv_and_install_wheel(
    tmp_path: Path, wheel_path: Path
) -> tuple[Path, Path]:
    """Create a fresh isolated venv, install the wheel, return (python, mnemos).

    Prefers ``uv venv`` + ``uv pip install`` when available; falls back to
    stdlib ``venv.create(with_pip=True)``. Mirrors ``test_inspect_packaging.py``.
    """
    venv_dir = tmp_path / "venv"
    bin_dirname = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    mnemos_name = "mnemos.exe" if os.name == "nt" else "mnemos"

    uv_path = shutil.which("uv")

    last_error: str | None = None

    if uv_path is not None:
        try:
            subprocess.run(
                [uv_path, "venv", "--python", "3.12", str(venv_dir)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    uv_path,
                    "pip",
                    "install",
                    "--python",
                    str(venv_dir / bin_dirname / python_name),
                    str(wheel_path),
                ],
                check=True,
                capture_output=True,
            )
            venv_python = venv_dir / bin_dirname / python_name
            venv_mnemos = venv_dir / bin_dirname / mnemos_name
            assert venv_python.exists(), f"venv python not found at {venv_python}"
            assert venv_mnemos.exists(), (
                f"mnemos console script not installed at {venv_mnemos}"
            )
            return venv_python, venv_mnemos
        except (subprocess.CalledProcessError, AssertionError) as exc:
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
                else str(exc)
            )
            last_error = f"uv path failed: {stderr.strip()}"
            if venv_dir.exists():
                shutil.rmtree(venv_dir, ignore_errors=True)

    try:
        venv.create(str(venv_dir), with_pip=True, clear=True)
        venv_python = venv_dir / bin_dirname / python_name
        venv_mnemos = venv_dir / bin_dirname / mnemos_name
        subprocess.run(
            [str(venv_python), "-m", "pip", "install", str(wheel_path)],
            check=True,
            capture_output=True,
        )
        assert venv_python.exists(), f"venv python not found at {venv_python}"
        assert venv_mnemos.exists(), (
            f"mnemos console script not installed at {venv_mnemos}"
        )
        return venv_python, venv_mnemos
    except (subprocess.CalledProcessError, OSError, AssertionError) as exc:
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else str(exc)
        )
        suffix = f" Earlier {last_error}." if last_error else ""
        pytest.fail(
            "Could not create an isolated venv to install the built wheel. "
            f"stdlib venv.create failed: {stderr.strip()}.{suffix} "
            "Install 'uv' or ensure ensurepip is available."
        )


# --------------------------------------------------------------------------- #
# T01 — wheel ships ui.html as package-data
# --------------------------------------------------------------------------- #
class TestWheelShipsUiTemplate:
    def test_wheel_namelist_includes_ui_html(self, tmp_path):
        """The existing ``core = ["templates/*.html"]`` glob must continue to
        capture every new ``.html`` under ``core/templates/``."""
        wheel_path = _build_wheel(tmp_path)

        with zipfile.ZipFile(wheel_path) as zf:
            namelist = zf.namelist()

        assert TEMPLATE_REL_PATH in namelist, (
            f"{TEMPLATE_REL_PATH} not shipped in wheel. "
            f"Wheel contents: {sorted(namelist)}"
        )


# --------------------------------------------------------------------------- #
# T02 — wheel METADATA declares the optional [ui] extra (no pywebview install)
# --------------------------------------------------------------------------- #
class TestWheelDeclaresUiExtra:
    def test_metadata_declares_provides_extra_and_requires_dist(self, tmp_path):
        wheel_path = _build_wheel(tmp_path)

        with zipfile.ZipFile(wheel_path) as zf:
            metadata_names = [
                n for n in zf.namelist() if n.endswith(".dist-info/METADATA")
            ]
            assert metadata_names, "no *.dist-info/METADATA in wheel"
            metadata = zf.read(metadata_names[0]).decode("utf-8")

        assert "Provides-Extra: ui" in metadata, (
            "wheel METADATA must declare 'Provides-Extra: ui'. "
            f"METADATA:\n{metadata}"
        )
        # Requires-Dist line scoped to the ui extra naming pywebview.
        req_pattern = re.compile(
            r'^Requires-Dist:\s*pywebview.*;\s*extra\s*==\s*["\']ui["\']',
            re.MULTILINE,
        )
        assert req_pattern.search(metadata), (
            "wheel METADATA must carry a "
            "'Requires-Dist: pywebview...; extra == \"ui\"' line. "
            f"METADATA:\n{metadata}"
        )


# --------------------------------------------------------------------------- #
# T03 — installed wheel exposes ui.html via importlib.resources
# --------------------------------------------------------------------------- #
class TestInstalledWheelLoadsUiTemplate:
    def test_importlib_resources_reads_ui_template_from_installed_wheel(
        self, tmp_path
    ):
        wheel_path = _build_wheel(tmp_path)
        venv_python, _ = _create_venv_and_install_wheel(tmp_path, wheel_path)

        probe = (
            "from importlib.resources import files; "
            "text = files('core.templates').joinpath('ui.html')"
            ".read_text('utf-8'); "
            "print(len(text)); "
            "print(text[:80])"
        )
        result = subprocess.run(
            [str(venv_python), "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )

        stdout = result.stdout.strip().splitlines()
        assert stdout, "in-venv probe produced no output"
        size = int(stdout[0])
        assert size > 0, "template was empty when read via importlib.resources"


# --------------------------------------------------------------------------- #
# T04 — `mnemos ui --output` CLI works end-to-end inside the installed venv
# --------------------------------------------------------------------------- #
class TestMnemosUiCliInVenv:
    def test_mnemos_ui_output_produces_non_empty_html_in_venv(self, tmp_path):
        if not SAMPLE_REPO_ROOT.is_dir():
            pytest.skip(
                f"Sample mnemos repo not present at {SAMPLE_REPO_ROOT}; "
                "test requires a known-good repo for the CLI to read."
            )

        wheel_path = _build_wheel(tmp_path)
        _, venv_mnemos = _create_venv_and_install_wheel(tmp_path, wheel_path)

        out_html = tmp_path / "out.html"
        env = os.environ.copy()
        env["MNEMOS_REPO_ROOT"] = str(SAMPLE_REPO_ROOT)

        # --output is the headless path: it must succeed WITHOUT pywebview
        # installed in this venv (the [ui] extra was never installed).
        subprocess.run(
            [str(venv_mnemos), "ui", "--output", str(out_html)],
            check=True,
            capture_output=True,
            env=env,
        )

        assert out_html.exists(), f"mnemos ui did not write {out_html}"
        assert out_html.stat().st_size > 0, "mnemos ui produced empty HTML"
        contents = out_html.read_text(encoding="utf-8")
        assert '<script id="ui-data"' in contents, (
            "rendered HTML missing ui-data block — template likely empty "
            "or wrong template substituted"
        )
        assert '<canvas id="graph"' in contents, (
            "rendered HTML missing graph canvas — Graph tab target lost"
        )
