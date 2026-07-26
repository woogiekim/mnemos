#!/usr/bin/env python3
"""Fast Stop hook entrypoint."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    if not repo_root:
        return 0

    mnemos_bin = shutil.which("mnemos")
    if not mnemos_bin:
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    session_id = str(payload.get("session_id") or "")
    transcript_path = str(payload.get("transcript_path") or "")
    if not transcript_path or not Path(transcript_path).is_file():
        return 0

    script_dir = Path(__file__).resolve().parent
    source_root = script_dir.parent
    pythonpath = (
        f"{source_root}{os.pathsep}{os.environ['PYTHONPATH']}"
        if os.environ.get("PYTHONPATH")
        else str(source_root)
    )
    try:
        subprocess.Popen(
            [
                mnemos_bin,
                "capture-transcript",
                "--json",
                "--transcript-path",
                transcript_path,
                "--session-id",
                session_id,
                "--host",
                "claude-code",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "MNEMOS_REPO_ROOT": repo_root, "PYTHONPATH": pythonpath},
            start_new_session=True,
        )
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
