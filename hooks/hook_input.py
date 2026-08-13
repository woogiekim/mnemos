#!/usr/bin/env python3
"""Read one host hook payload without requiring stdin EOF."""
from __future__ import annotations

import json
import os
import select
import stat
import sys
import time


DEFAULT_IDLE_SECONDS = 0.2
MAX_BYTES = 1024 * 1024
READ_SIZE = 65536


def is_complete_json(payload: bytearray) -> bool:
    try:
        json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def read_available_stdin(
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
    max_bytes: int = MAX_BYTES,
    max_seconds: float | None = None,
) -> bytes:
    """Return delivered stdin bytes after EOF or a short idle window."""
    try:
        fd = sys.stdin.fileno()
        mode = os.fstat(fd).st_mode
    except (AttributeError, OSError, ValueError):
        return b""

    if not (stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISSOCK(mode)):
        return b""

    try:
        os.set_blocking(fd, False)
    except OSError:
        pass

    payload = bytearray()
    started_at = time.monotonic()
    idle_until = started_at + idle_seconds
    deadline = started_at + max_seconds if max_seconds is not None else None
    while len(payload) < max_bytes:
        try:
            chunk = os.read(fd, min(READ_SIZE, max_bytes - len(payload)))
        except BlockingIOError:
            remaining = idle_until - time.monotonic()
            if deadline is not None:
                remaining = min(remaining, deadline - time.monotonic())
            if remaining <= 0:
                break
            try:
                select.select([fd], [], [], min(0.02, remaining))
            except (OSError, ValueError):
                break
            continue
        except InterruptedError:
            continue
        except OSError:
            break

        if not chunk:
            break

        payload.extend(chunk)
        if is_complete_json(payload):
            return bytes(payload)
        idle_until = time.monotonic() + idle_seconds

    return bytes(payload)


def main() -> int:
    payload = read_available_stdin(idle_seconds=1.0, max_seconds=3.0)
    if not payload or not is_complete_json(bytearray(payload)):
        return 1

    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
