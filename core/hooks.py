"""Hook Dispatcher — fires shell scripts after memory mutations."""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_EVENTS = ["post-capture", "post-promote", "post-archive", "post-forget"]


class HookDispatcher:
    """Executes hook scripts found in .agent/workflows/hooks/{event_name}/."""

    def __init__(self, repo_root: str) -> None:
        self._root = Path(repo_root)
        self._hooks_dir = self._root / ".agent" / "workflows" / "hooks"

    def fire(self, event_name: str, payload: dict[str, Any]) -> None:
        """
        Run all hook scripts for the given event.

        Scripts are executed non-blocking. Errors are logged, not raised.

        Args:
            event_name: One of post-capture, post-promote, post-archive, post-forget.
            payload: Dict passed as JSON-encoded env vars to the script.
        """
        event_dir = self._hooks_dir / event_name
        if not event_dir.exists():
            return

        env_extras = {f"MNEMOS_{k.upper()}": str(v) for k, v in payload.items()}

        for script in sorted(event_dir.iterdir()):
            if not script.is_file():
                continue
            # Only execute files that are shell scripts (by convention)
            if not (script.suffix in (".sh", ".bash", "") or script.stat().st_mode & 0o111):
                continue
            self._run_script(script, env_extras)

    def _run_script(self, script: Path, env_extras: dict[str, str]) -> None:
        """Execute a single hook script asynchronously."""
        import os
        env = os.environ.copy()
        env.update(env_extras)
        try:
            proc = subprocess.Popen(
                [str(script)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            threading.Thread(target=proc.wait, daemon=True).start()
        except OSError as exc:
            logger.warning("Hook script failed to start: %s — %s", script, exc)
        except Exception as exc:
            logger.warning("Unexpected error running hook %s: %s", script, exc)
