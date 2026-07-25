#!/usr/bin/env python3
"""Background context prefetch worker for UserPromptSubmit hooks."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--exact-cache", required=True)
    parser.add_argument("--last-cache", required=True)
    args = parser.parse_args()

    env = os.environ.copy()
    env["MNEMOS_REPO_ROOT"] = args.repo_root
    cmd = [
        "mnemos",
        "context",
        "--render",
        "--read-only",
        "--no-grep",
        "--prompt",
        args.prompt,
        "--session-id",
        args.session_id,
        "--host",
        args.host,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        return 0

    for cache_path in (Path(args.exact_cache), Path(args.last_cache)):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp_path.write_text(result.stdout, encoding="utf-8")
        tmp_path.replace(cache_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
