"""Bounded stdin reads for host hook compatibility paths."""
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
) -> bytes:
    """Read delivered pipe data without requiring the writer to close stdin."""
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
    idle_until = time.monotonic() + idle_seconds
    while len(payload) < max_bytes:
        try:
            chunk = os.read(fd, min(READ_SIZE, max_bytes - len(payload)))
        except BlockingIOError:
            remaining = idle_until - time.monotonic()
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
