"""Wheel-build regression test for the ``mnemos graph`` packaging fix.

Pinned by ``${TASK_DIR}/context/prd.md`` § "Acceptance criteria" (issue from
PR #68 fallout). After PR #68 the HTML template at ``core/templates/graph.html``
was added to the source tree but NOT shipped in the built wheel because
``pyproject.toml`` was missing a ``[tool.setuptools.package-data]`` entry.
``core/graphview.py`` also used filesystem-bound ``Path(...).read_text(...)``
which is fragile in a wheel/zipapp layout.

This test exercises the full install path:

1. Build a wheel via ``python -m build --wheel`` to ``tmp_path``.
2. Open the wheel ZIP and assert ``core/templates/graph.html`` is in
   ``namelist()`` (proves the template ships as package-data).
3. Create a fresh isolated venv at ``tmp_path / "venv"``.
4. Install the built wheel into that venv.
5. From inside that venv, assert
   ``importlib.resources.files("core.templates").joinpath("graph.html")
   .read_text("utf-8")`` is non-empty (proves the loader works regardless
   of install layout — filesystem, wheel-zip, zipapp).
6. Run ``mnemos graph --no-open --output {tmp_path}/out.html`` from the
   venv against a known-good sample mnemos repo and assert the output
   file exists and is non-empty.

The test must FAIL on current ``main`` (HEAD before the fix lands) and
PASS after the pyproject.toml + ``importlib.resources`` fix is applied.

Runtime: ~10-30s (wheel build + venv create + pip install). Runs in the
default suite — no opt-in mark.

Venv tooling: prefers ``uv`` when available (faster, no ``ensurepip``
bootstrap), and falls back to the stdlib ``venv.create(with_pip=True)``
path otherwise. When neither tooling is available — or when the bootstrap
actively errors out — the test fails loudly with a clear message rather
than skipping silently, so a missing CI dependency is visible.
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
TEMPLATE_REL_PATH = "core/templates/graph.html"
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


def _create_venv_and_install_wheel(tmp_path: Path, wheel_path: Path) -> tuple[Path, Path]:
    """Create a fresh isolated venv, install the wheel, return (python, mnemos).

    Strategy:
      1. Prefer ``uv venv`` + ``uv pip install`` — faster and avoids
         ``ensurepip`` bootstrap issues that some non-relocatable bundled
         Python interpreters exhibit.
      2. Fall back to stdlib ``venv.create(with_pip=True)`` + the venv's
         own ``pip``.

    Either path must produce a venv with the wheel installed; otherwise
    the test fails with a clear message (no silent skips).
    """
    venv_dir = tmp_path / "venv"
    bin_dirname = "Scripts" if os.name == "nt" else "bin"
    python_name = "python.exe" if os.name == "nt" else "python"
    mnemos_name = "mnemos.exe" if os.name == "nt" else "mnemos"

    uv_path = shutil.which("uv")

    last_error: str | None = None

    # Path 1: uv (preferred when available)
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
                f"mnemos console script not installed at {venv_mnemos} "
                "(check [project.scripts] in pyproject.toml)"
            )
            return venv_python, venv_mnemos
        except (subprocess.CalledProcessError, AssertionError) as exc:
            # Fall through to stdlib venv path; capture for the final error.
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
                else str(exc)
            )
            last_error = f"uv path failed: {stderr.strip()}"
            # Clean any partial venv before retrying with stdlib path.
            if venv_dir.exists():
                shutil.rmtree(venv_dir, ignore_errors=True)

    # Path 2: stdlib venv + venv's own pip
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
            f"mnemos console script not installed at {venv_mnemos} "
            "(check [project.scripts] in pyproject.toml)"
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
            "Install 'uv' (https://docs.astral.sh/uv/) or ensure the "
            "interpreter running pytest can bootstrap pip via ensurepip."
        )


# --------------------------------------------------------------------------- #
# T01 — wheel ships the HTML template as package-data
# --------------------------------------------------------------------------- #
class TestWheelShipsTemplate:
    def test_wheel_namelist_includes_graph_html(self, tmp_path):
        """The built wheel must contain core/templates/graph.html.

        Pre-fix this fails because pyproject.toml lacks
        ``[tool.setuptools.package-data]`` so only ``.py`` files end up in
        the wheel. After the fix, the pattern ``core = ["templates/*.html"]``
        ensures every ``.html`` file under ``core/templates/`` ships.
        """
        wheel_path = _build_wheel(tmp_path)

        with zipfile.ZipFile(wheel_path) as zf:
            namelist = zf.namelist()

        assert TEMPLATE_REL_PATH in namelist, (
            f"{TEMPLATE_REL_PATH} not shipped in wheel. "
            f"Wheel contents: {sorted(namelist)}"
        )


# --------------------------------------------------------------------------- #
# T02 — installed wheel exposes the template via importlib.resources
# --------------------------------------------------------------------------- #
class TestInstalledWheelLoadsTemplate:
    def test_importlib_resources_reads_template_from_installed_wheel(
        self, tmp_path
    ):
        """After installing the wheel into a fresh venv, the in-venv Python
        must be able to read the template via importlib.resources.

        This proves the loader works regardless of install layout (filesystem,
        wheel-zip, zipapp) — the central guarantee the fix delivers.
        """
        wheel_path = _build_wheel(tmp_path)
        venv_python, _ = _create_venv_and_install_wheel(tmp_path, wheel_path)

        # Read the template from inside the fresh venv using importlib.resources.
        probe = (
            "from importlib.resources import files; "
            "text = files('core.templates').joinpath('graph.html')"
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
# T03 — `mnemos graph` CLI works end-to-end inside the installed venv
# --------------------------------------------------------------------------- #
class TestMnemosGraphCliInVenv:
    def test_mnemos_graph_produces_non_empty_html_in_venv(self, tmp_path):
        """Running ``mnemos graph --no-open --output OUT`` inside a fresh
        venv against the sample mnemos repo must produce a non-empty HTML
        file. Pre-fix this fails with FileNotFoundError because the wheel
        does not ship the template.
        """
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
                "graph",
                "--no-open",
                "--output",
                str(out_html),
            ],
            check=True,
            capture_output=True,
            env=env,
        )

        assert out_html.exists(), f"mnemos graph did not write {out_html}"
        assert out_html.stat().st_size > 0, "mnemos graph produced empty HTML"
        # The rendered HTML must include the canvas renderer skeleton — pinned
        # in design-spec §8 and exercised by tests/test_graphview.py.
        contents = out_html.read_text(encoding="utf-8")
        assert '<canvas id="graph"' in contents, (
            "rendered HTML missing canvas skeleton — template likely empty "
            "or wrong template substituted"
        )
