"""macOS launchd daemon management for automated mnemos GC.

Installs / uninstalls a launchd plist at
``~/Library/LaunchAgents/com.mnemos.gc.plist`` that runs
``mnemos bg-check --quiet`` daily at 3 AM.

GC output is appended to ``~/.mnemos/.logs/gc.log`` and
``~/.mnemos/.logs/gc-error.log``.

Public API
----------
- ``install_gc_daemon()``   — write plist and run ``launchctl load``
- ``uninstall_gc_daemon()`` — run ``launchctl unload`` and remove plist
- ``manage_gc_daemon(install, uninstall)`` — CLI entry point
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLIST_LABEL = "com.mnemos.gc"
PLIST_FILENAME = f"{PLIST_LABEL}.plist"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = LAUNCH_AGENTS_DIR / PLIST_FILENAME
GC_LOG_DIR = Path.home() / ".mnemos" / ".logs"
GC_LOG_PATH = GC_LOG_DIR / "gc.log"
GC_ERROR_LOG_PATH = GC_LOG_DIR / "gc-error.log"


# ---------------------------------------------------------------------------
# Plist template
# ---------------------------------------------------------------------------

def _build_plist(mnemos_bin: str) -> str:
    """Return the launchd plist XML for the GC daemon.

    Parameters
    ----------
    mnemos_bin:
        Absolute path to the ``mnemos`` executable.
    """
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>{mnemos_bin}</string>
    <string>bg-check</string>
    <string>--quiet</string>
  </array>

  <!-- Run daily at 3:00 AM -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>{GC_LOG_PATH}</string>

  <key>StandardErrorPath</key>
  <string>{GC_ERROR_LOG_PATH}</string>

  <!-- Prevent launchd from restarting the job on failure -->
  <key>RunAtLoad</key>
  <false/>
</dict>
</plist>
"""


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def install_gc_daemon() -> None:
    """Install the GC launchd daemon on macOS.

    1. Resolves the ``mnemos`` binary path.
    2. Creates ``~/.mnemos/.logs/`` if absent.
    3. Writes ``~/Library/LaunchAgents/com.mnemos.gc.plist``.
    4. Calls ``launchctl load -w <plist>`` to register and enable the job.

    Raises
    ------
    RuntimeError
        On non-macOS systems.
    FileNotFoundError
        When the ``mnemos`` binary cannot be found on PATH.
    subprocess.CalledProcessError
        When ``launchctl load`` fails.
    """
    _require_macos()

    # Resolve the mnemos binary
    mnemos_bin = shutil.which("mnemos")
    if not mnemos_bin:
        raise FileNotFoundError(
            "mnemos binary not found on PATH. "
            "Make sure mnemos is installed (pipx install mnemos)."
        )

    # Ensure log directory exists
    GC_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure LaunchAgents directory exists (it always should, but be safe)
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    # Write the plist
    plist_content = _build_plist(mnemos_bin)
    PLIST_PATH.write_text(plist_content, encoding="utf-8")
    click.echo(f"[mnemos gc] plist written: {PLIST_PATH}")

    # If an old job is already loaded, unload it first (idempotent reinstall)
    _launchctl_unload_if_loaded()

    # Load (register) the new plist
    subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_PATH)],
        check=True,
    )
    click.echo(f"[mnemos gc] daemon loaded: {PLIST_LABEL}")
    click.echo(f"[mnemos gc] schedule: daily at 03:00")
    click.echo(f"[mnemos gc] log: {GC_LOG_PATH}")
    click.echo(f"[mnemos gc] error log: {GC_ERROR_LOG_PATH}")
    click.echo(
        "[mnemos gc] Run 'mnemos gc --uninstall-daemon' to remove the daemon."
    )


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

def uninstall_gc_daemon() -> None:
    """Unload and remove the GC launchd daemon on macOS.

    1. Calls ``launchctl unload -w <plist>`` (non-fatal if not loaded).
    2. Removes the plist file.

    Raises
    ------
    RuntimeError
        On non-macOS systems or when the plist file does not exist.
    """
    _require_macos()

    if not PLIST_PATH.exists():
        raise FileNotFoundError(
            f"Daemon plist not found: {PLIST_PATH}\n"
            "Run 'mnemos gc --install-daemon' first."
        )

    # Unload the job (best-effort — not fatal if not currently loaded)
    try:
        subprocess.run(
            ["launchctl", "unload", "-w", str(PLIST_PATH)],
            check=True,
        )
        click.echo(f"[mnemos gc] daemon unloaded: {PLIST_LABEL}")
    except subprocess.CalledProcessError as exc:
        # Non-fatal: job may not have been loaded (e.g. after a reboot without reload)
        click.echo(
            f"[mnemos gc] warning: launchctl unload returned rc={exc.returncode} "
            "(continuing with plist removal)",
            err=True,
        )

    PLIST_PATH.unlink()
    click.echo(f"[mnemos gc] plist removed: {PLIST_PATH}")
    click.echo("[mnemos gc] daemon uninstalled.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def manage_gc_daemon(*, install: bool, uninstall: bool) -> None:
    """CLI dispatcher called by ``mnemos gc --install-daemon / --uninstall-daemon``.

    Exactly one of *install* or *uninstall* must be True.  Both True is a
    usage error; both False is a no-op (caller should not reach here).
    """
    if install and uninstall:
        click.echo(
            "error: --install-daemon and --uninstall-daemon are mutually exclusive.",
            err=True,
        )
        sys.exit(1)

    try:
        if install:
            install_gc_daemon()
        elif uninstall:
            uninstall_gc_daemon()
    except RuntimeError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except FileNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        click.echo(
            f"error: launchctl failed (rc={exc.returncode}): {exc.stderr or ''}",
            err=True,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_macos() -> None:
    """Raise RuntimeError on non-macOS systems."""
    if platform.system() != "Darwin":
        raise RuntimeError(
            "Launchd daemon management is only supported on macOS. "
            f"Current platform: {platform.system()}"
        )


def _launchctl_unload_if_loaded() -> None:
    """Unload the job if it is currently registered — silently ignore errors."""
    try:
        subprocess.run(
            ["launchctl", "unload", "-w", str(PLIST_PATH)],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass  # launchctl not available or job not loaded — ignore
