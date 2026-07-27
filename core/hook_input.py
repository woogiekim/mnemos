"""Bounded stdin reads for host hook compatibility paths."""
from __future__ import annotations

import os
import select
import stat
import sys
import time


DEFAULT_IDLE_SECONDS = 0.2
READ_SIZE = 65536


def read_available_stdin(idle_seconds: float = DEFAULT_IDLE_SECONDS) -> bytes:
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

    chunks: list[bytes] = []
    idle_until = time.monotonic() + idle_seconds
    while True:
        try:
            chunk = os.read(fd, READ_SIZE)
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

        chunks.append(chunk)
        idle_until = time.monotonic() + idle_seconds

    return b"".join(chunks)
