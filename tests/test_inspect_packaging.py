"""Wheel-build regression test for ``mnemos inspect`` packaging (issue #80).

Mirrors :mod:`tests.test_graph_packaging` (issue #68). The new
``core/templates/inspect.html`` MUST be shipped in the built wheel via the
existing ``[tool.setuptools.package-data] core = ["templates/*.html"]`` glob,
and the in-venv ``mnemos inspect`` command MUST be able to load it via
``importlib.resources.files("core.templates").joinpath("inspect.html")
.read_text("utf-8")``.

This regression test exercises the same install path issue #68 fixed:

1. Build a wheel via ``python -m build --wheel`` to ``tmp_path``.
2. Open the wheel ZIP and assert ``core/templates/inspect.html`` is in
   ``namelist()`` (proves the template ships as package-data).
3. Create a fresh isolated venv at ``tmp_path / "venv"``.
4. Install the built wheel into that venv.
5. From inside that venv, assert
   ``importlib.resources.files("core.templates").joinpath("inspect.html")
   .read_text("utf-8")`` is non-empty.
6. Run ``mnemos inspect --no-open --output {tmp_path}/out.html`` from the
   venv against the known-good sample mnemos repo and assert the output
   file exists, is non-empty, and contains the inspect-data script block.

Runtime: ~10-30s (wheel build + venv create + pip install). No opt-in mark.
"""
from __future__ import annotations

import os
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
TEMPLATE_REL_PATH = "core/templates/inspect.html"
SAMPLE_REPO_ROOT = PROJECT_ROOT / "repo"


def _build_wheel(tmp_path: Path) -> Path:
    """Build a wheel from PROJECT_ROOT into tmp_path; return the .whl path."""
    outdir = tmp_path / "dist"
    outdir.mkdir()

    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        check=True,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
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
    stdlib ``venv.create(with_pip=True)``. Mirrors ``test_graph_packaging.py``.
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
# T01 — wheel ships inspect.html as package-data
# --------------------------------------------------------------------------- #
class TestWheelShipsInspectTemplate:
    def test_wheel_namelist_includes_inspect_html(self, tmp_path):
        """The existing ``core = ["templates/*.html"]`` glob must continue
        to capture every new ``.html`` under ``core/templates/``."""
        wheel_path = _build_wheel(tmp_path)

        with zipfile.ZipFile(wheel_path) as zf:
            namelist = zf.namelist()

        assert TEMPLATE_REL_PATH in namelist, (
            f"{TEMPLATE_REL_PATH} not shipped in wheel. "
            f"Wheel contents: {sorted(namelist)}"
        )


# --------------------------------------------------------------------------- #
# T02 — installed wheel exposes inspect.html via importlib.resources
# --------------------------------------------------------------------------- #
class TestInstalledWheelLoadsInspectTemplate:
    def test_importlib_resources_reads_inspect_template_from_installed_wheel(
        self, tmp_path
    ):
        wheel_path = _build_wheel(tmp_path)
        venv_python, _ = _create_venv_and_install_wheel(tmp_path, wheel_path)

        probe = (
            "from importlib.resources import files; "
            "text = files('core.templates').joinpath('inspect.html')"
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
# T03 — `mnemos inspect` CLI works end-to-end inside the installed venv
# --------------------------------------------------------------------------- #
class TestMnemosInspectCliInVenv:
    def test_mnemos_inspect_produces_non_empty_html_in_venv(self, tmp_path):
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

        subprocess.run(
            [
                str(venv_mnemos),
                "inspect",
                "--no-open",
                "--output",
                str(out_html),
            ],
            check=True,
            capture_output=True,
            env=env,
        )

        assert out_html.exists(), f"mnemos inspect did not write {out_html}"
        assert out_html.stat().st_size > 0, "mnemos inspect produced empty HTML"
        contents = out_html.read_text(encoding="utf-8")
        # Inspect-data block + a visible search affordance (AC1 anchor).
        assert '<script id="inspect-data"' in contents, (
            "rendered HTML missing inspect-data block — template likely empty "
            "or wrong template substituted"
        )
        assert 'role="search"' in contents, (
            "rendered HTML missing search affordance — AC1 anchor lost"
        )
