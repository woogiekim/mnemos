"""Audit Logger — append-only log in human-readable and machine-readable formats."""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Any


class AuditLogger:
    """Thread-safe append-only logger to wiki/log.md and wiki/log.jsonl."""

    def __init__(self, repo_root: str) -> None:
        self._root = Path(repo_root)
        self._log_md = self._root / "wiki" / "log.md"
        self._log_jsonl = self._root / "wiki" / "log.jsonl"
        self._lock = threading.Lock()
        self._ensure_log_files()

    def _ensure_log_files(self) -> None:
        """Create log files if they don't exist."""
        self._log_md.parent.mkdir(parents=True, exist_ok=True)
        if not self._log_md.exists():
            self._log_md.write_text("# mnemos Audit Log\n\n---\n")
        if not self._log_jsonl.exists():
            self._log_jsonl.write_text("")

    def append(
        self,
        operation: str,
        item_id: str,
        layer: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit log entry to both log.md and log.jsonl."""
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.isoformat() + "Z"
        meta = metadata or {}

        entry = {
            "timestamp": ts,
            "operation": operation,
            "item_id": item_id,
            "layer": layer,
            **meta,
        }

        with self._lock:
            # Human-readable append
            human_line = f"[{ts}] {operation.upper()} {item_id} ({layer})"
            if meta:
                human_line += f" — {meta}"
            human_line += "\n"
            with self._log_md.open("a", encoding="utf-8") as f:
                f.write(human_line)

            # Machine-readable append
            with self._log_jsonl.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
