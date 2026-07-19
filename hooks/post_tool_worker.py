"""Detached, single-flight worker for Claude PostToolUse maintenance."""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
from pathlib import Path


def env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def is_recent(path: Path, threshold_seconds: int) -> bool:
    try:
        return time.time() - path.stat().st_mtime < threshold_seconds
    except OSError:
        return False


def main() -> int:
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "").strip()
    if not repo_root:
        return 0

    uid = os.getuid() if hasattr(os, "getuid") else 0
    interval = max(0, int(os.environ.get("MNEMOS_BG_INTERVAL_MINUTES", "5")))
    worker_timeout = max(
        1, int(os.environ.get("MNEMOS_BG_WORKER_TIMEOUT_SECONDS", "300"))
    )
    timestamp = env_path("MNEMOS_BG_TS_FILE", f"/tmp/mnemos-bg-check-{uid}.ts")
    lock_path = env_path("MNEMOS_BG_LOCK_FILE", f"/tmp/mnemos-bg-check-{uid}.lock")
    result_path = env_path(
        "MNEMOS_BG_RESULT_FILE", f"/tmp/mnemos-bg-check-{uid}.result"
    )

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        if is_recent(timestamp, interval * 60):
            return 0

        timestamp.parent.mkdir(parents=True, exist_ok=True)
        timestamp.touch()
        try:
            completed = subprocess.run(
                ["mnemos", "bg-check", "--force", "--interval", str(interval)],
                env={**os.environ, "MNEMOS_REPO_ROOT": repo_root},
                text=True,
                capture_output=True,
                check=False,
                timeout=worker_timeout,
            )
        except subprocess.TimeoutExpired:
            return 0
        output = completed.stdout.strip()
        if output:
            result_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = result_path.with_name(f"{result_path.name}.worker.{os.getpid()}")
            temporary.write_text(f"{output}\n", encoding="utf-8")
            os.replace(temporary, result_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
