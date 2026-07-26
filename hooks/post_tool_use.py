#!/usr/bin/env python3
"""Fast PostToolUse hook entrypoint."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def _drain_result(result_file: Path) -> None:
    if not result_file.is_file():
        return
    drain_file = result_file.with_name(f"{result_file.name}.drain.{os.getpid()}")
    try:
        result_file.replace(drain_file)
    except OSError:
        return
    try:
        sys.stdout.write(drain_file.read_text(encoding="utf-8"))
    except OSError:
        pass
    finally:
        try:
            drain_file.unlink()
        except OSError:
            pass


def main() -> int:
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    if not repo_root:
        return 0
    if not shutil.which("mnemos"):
        return 0

    uid = os.getuid() if hasattr(os, "getuid") else 0
    interval = os.environ.get("MNEMOS_BG_INTERVAL_MINUTES", "5")
    ts_file = os.environ.get("MNEMOS_BG_TS_FILE", f"/tmp/mnemos-bg-check-{uid}.ts")
    lock_file = os.environ.get("MNEMOS_BG_LOCK_DIR", f"/tmp/mnemos-bg-check-{uid}.lock")
    result_file = Path(os.environ.get("MNEMOS_BG_RESULT_FILE", f"/tmp/mnemos-bg-check-{uid}.result"))

    _drain_result(result_file)

    script_dir = Path(__file__).resolve().parent
    try:
        subprocess.Popen(
            [sys.executable, str(script_dir / "post_tool_worker.py")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "MNEMOS_REPO_ROOT": repo_root,
                "MNEMOS_BG_INTERVAL_MINUTES": interval,
                "MNEMOS_BG_TS_FILE": ts_file,
                "MNEMOS_BG_LOCK_FILE": lock_file,
                "MNEMOS_BG_RESULT_FILE": str(result_file),
            },
            start_new_session=True,
        )
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
