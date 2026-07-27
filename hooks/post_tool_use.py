#!/usr/bin/env python3
"""Fast PostToolUse hook entrypoint."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from hook_input import read_available_stdin


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


def _start_claude_md_ingest(payload: dict, mnemos_bin: str) -> None:
    if payload.get("tool_name") not in {"Write", "Edit", "MultiEdit"}:
        return

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    raw_path = (
        tool_input.get("file_path")
        or payload.get("file_path")
        or payload.get("path")
        or ""
    )
    changed_path = Path(str(raw_path)).expanduser()
    if changed_path.name != "CLAUDE.md":
        return

    project_root = str(payload.get("cwd") or changed_path.parent)
    try:
        subprocess.Popen(
            [
                mnemos_bin,
                "ingest-claude-md",
                "--project-root",
                project_root,
                "--skip-files",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
        )
    except OSError:
        pass


def main() -> int:
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    if not repo_root:
        return 0
    mnemos_bin = shutil.which("mnemos")
    if not mnemos_bin:
        return 0

    try:
        payload = json.loads(read_available_stdin() or b"{}")
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        _start_claude_md_ingest(payload, mnemos_bin)

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
