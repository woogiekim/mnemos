"""Observability logger — append-only JSONL event log for mnemos usage tracking.

Records every hook invocation, capture, search, GC, and promotion event so that
users can verify how AI agents (Claude and others) actually use the memory system.

Log file location: {repo_root}/.agent/observability.jsonl

The log lives under the gitignored ``.agent/`` tree (not ``wiki/``) so its raw
search keywords are never staged by the git-sync wiki filter or pushed to a
remote (issue #77, Finding F1).

Schema (one JSON object per line):
    {
        "ts":         "ISO8601Z",           # UTC timestamp
        "event":      "hook_search | hook_session_start | capture | search |
                       gc | promotion | hook_post_tool",
        "agent":      "claude | cursor | unknown",
        "session_id": "...",                # Claude session ID or empty string
        "keywords":   ["kw1", "kw2"],      # for search/hook_search events
        "results":    [{"id": "...", "score": 0.0}],  # for search events
        "result_count": 0,                 # convenience count
        "layer":      "ephemeral | ...",   # for capture/promotion events
        "memory_id":  "...",               # for capture/promotion/gc events
        "from_layer": "...",               # for promotion events
        "archived_count": 0,               # for gc events
        "tags":       [],                  # for capture events
        "extra":      {}                   # reserved for future fields
    }

Design goals:
- Agent-agnostic: every entry has an "agent" field so other AI agents can be tracked.
- Non-blocking: writes happen in a background thread so hook latency is unaffected.
- Minimal overhead: a single append per event, no external dependencies.
- Compatible with PostToolUse hook (background-ops branch) via the "hook_post_tool" event.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
from pathlib import Path
from typing import Any


class ObservabilityLogger:
    """Thread-safe append-only logger to .agent/observability.jsonl.

    All public methods are fire-and-forget: they enqueue a write to a
    background thread so callers (hooks, gateway) never block on disk I/O.
    """

    def __init__(self, repo_root: str) -> None:
        self._root = Path(repo_root)
        # The observability log lives under the gitignored ``.agent/`` tree, not
        # ``wiki/`` — keeping it out of the git-synced sub-tree so raw search
        # keywords are never staged or pushed to a remote (issue #77, F1).
        self._log_path = self._root / ".agent" / "observability.jsonl"
        self._lock = threading.Lock()
        self._ensure_log_file()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                      #
    # ------------------------------------------------------------------ #

    def _ensure_log_file(self) -> None:
        """Create the log file if it does not exist (mkdir -p for parent)."""
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._log_path.exists():
                self._log_path.touch()
        except Exception:
            pass  # never crash the caller

    @staticmethod
    def _now_iso() -> str:
        return datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def _write(self, entry: dict[str, Any]) -> None:
        """Append a JSON line synchronously (called from background thread)."""
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass  # observability must never crash the caller

    def _write_async(self, entry: dict[str, Any]) -> None:
        """Enqueue a background write so callers are never blocked."""
        t = threading.Thread(target=self._write, args=(entry,), daemon=True)
        t.start()

    @staticmethod
    def _base(
        event: str,
        agent: str = "unknown",
        session_id: str = "",
    ) -> dict[str, Any]:
        return {
            "ts": ObservabilityLogger._now_iso(),
            "event": event,
            "agent": agent,
            "session_id": session_id,
        }

    # ------------------------------------------------------------------ #
    # Public API                                                            #
    # ------------------------------------------------------------------ #

    def log_hook_session_start(
        self,
        session_id: str = "",
        agent: str = "claude",
        memory_count: int = 0,
    ) -> None:
        """Record a UserPromptSubmit session-start context load."""
        entry = self._base("hook_session_start", agent=agent, session_id=session_id)
        entry["memory_count"] = memory_count
        self._write_async(entry)

    def log_hook_search(
        self,
        keywords: list[str],
        results: list[dict[str, Any]],
        session_id: str = "",
        agent: str = "claude",
    ) -> None:
        """Record a hook-triggered mnemos search (UserPromptSubmit active search)."""
        entry = self._base("hook_search", agent=agent, session_id=session_id)
        entry["keywords"] = keywords
        # Store lightweight result references (id + score) to avoid giant log lines
        entry["results"] = [
            {"id": r.get("item_id", ""), "score": r.get("score", 0.0)}
            for r in results
        ]
        entry["result_count"] = len(results)
        self._write_async(entry)

    def log_capture(
        self,
        memory_id: str,
        layer: str,
        tags: list[str] | None = None,
        session_id: str = "",
        agent: str = "claude",
    ) -> None:
        """Record a mnemos capture call."""
        entry = self._base("capture", agent=agent, session_id=session_id)
        entry["memory_id"] = memory_id
        entry["layer"] = layer
        entry["tags"] = tags or []
        self._write_async(entry)

    def log_search(
        self,
        keywords: list[str],
        results: list[dict[str, Any]],
        session_id: str = "",
        agent: str = "unknown",
    ) -> None:
        """Record a programmatic search (via gateway.search())."""
        entry = self._base("search", agent=agent, session_id=session_id)
        entry["keywords"] = keywords
        entry["results"] = [
            {"id": r.get("item_id", ""), "score": r.get("score", 0.0)}
            for r in results
        ]
        entry["result_count"] = len(results)
        self._write_async(entry)

    def log_gc(
        self,
        archived_count: int,
        dry_run: bool = False,
        layers: list[str] | None = None,
        session_id: str = "",
        agent: str = "unknown",
    ) -> None:
        """Record a garbage-collection run."""
        entry = self._base("gc", agent=agent, session_id=session_id)
        entry["archived_count"] = archived_count
        entry["dry_run"] = dry_run
        entry["layers"] = layers or []
        self._write_async(entry)

    def log_promotion(
        self,
        memory_id: str,
        from_layer: str,
        to_layer: str,
        session_id: str = "",
        agent: str = "unknown",
    ) -> None:
        """Record a promotion event."""
        entry = self._base("promotion", agent=agent, session_id=session_id)
        entry["memory_id"] = memory_id
        entry["from_layer"] = from_layer
        entry["layer"] = to_layer
        self._write_async(entry)

    def log_hook_post_tool(
        self,
        tool_name: str = "",
        session_id: str = "",
        agent: str = "claude",
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Record a PostToolUse hook invocation (agent-agnostic, future-compat)."""
        entry = self._base("hook_post_tool", agent=agent, session_id=session_id)
        entry["tool_name"] = tool_name
        if extra:
            entry["extra"] = extra
        self._write_async(entry)

    # ------------------------------------------------------------------ #
    # Read / aggregate API (used by mnemos audit and mnemos stats)          #
    # ------------------------------------------------------------------ #

    def read_entries(
        self,
        tail: int | None = None,
        session_id: str | None = None,
        events: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read log entries from observability.jsonl with optional filtering.

        Parameters
        ----------
        tail:
            When set, return only the last *tail* entries (after filtering).
        session_id:
            Filter to entries matching this session_id.
        events:
            Filter to entries whose ``event`` field is in this list.
        """
        if not self._log_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if session_id and entry.get("session_id") != session_id:
                        continue
                    if events and entry.get("event") not in events:
                        continue
                    entries.append(entry)
        except Exception:
            return []

        if tail is not None:
            entries = entries[-tail:]

        return entries

    def aggregate_stats(self, days: int = 7) -> dict[str, Any]:
        """Aggregate usage statistics over the last *days* days.

        Returns a dict with:
            captures_by_layer      — {layer: count}
            searches_per_day       — {YYYY-MM-DD: count}
            top_keywords           — [(keyword, count), ...] top 5
            top_surfaced_memories  — [(memory_id, count), ...] top 5
            last_gc_ts             — ISO8601 string or None
            last_gc_count          — int
            hook_calls             — total hook_search + hook_session_start count
            total_entries          — total log entries
        """
        import collections

        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days
        )
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        captures_by_layer: dict[str, int] = collections.defaultdict(int)
        searches_per_day: dict[str, int] = collections.defaultdict(int)
        keyword_counts: dict[str, int] = collections.defaultdict(int)
        memory_surface_counts: dict[str, int] = collections.defaultdict(int)
        last_gc_ts: str | None = None
        last_gc_count: int = 0
        hook_calls: int = 0
        total_entries: int = 0

        all_entries = self.read_entries()
        total_entries = len(all_entries)

        for entry in all_entries:
            ts = entry.get("ts", "")
            event = entry.get("event", "")
            in_window = ts >= cutoff_str

            if event == "capture" and in_window:
                layer = entry.get("layer", "unknown")
                captures_by_layer[layer] += 1

            elif event in ("hook_search", "search") and in_window:
                day = ts[:10]  # YYYY-MM-DD
                searches_per_day[day] += 1
                for kw in entry.get("keywords", []):
                    keyword_counts[kw.lower()] += 1
                for r in entry.get("results", []):
                    mid = r.get("id", "")
                    if mid:
                        memory_surface_counts[mid] += 1

            elif event in ("hook_search", "hook_session_start") and in_window:
                hook_calls += 1

            elif event == "gc":
                # Track last GC regardless of window
                if last_gc_ts is None or ts > last_gc_ts:
                    last_gc_ts = ts
                    last_gc_count = entry.get("archived_count", 0)

        # Also count hook_search in hook_calls (duplicated above — fix: count once)
        hook_calls = sum(
            1
            for e in all_entries
            if e.get("event") in ("hook_search", "hook_session_start")
            and e.get("ts", "") >= cutoff_str
        )

        top_keywords = sorted(
            keyword_counts.items(), key=lambda x: -x[1]
        )[:5]
        top_surfaced = sorted(
            memory_surface_counts.items(), key=lambda x: -x[1]
        )[:5]

        return {
            "captures_by_layer": dict(captures_by_layer),
            "searches_per_day": dict(searches_per_day),
            "top_keywords": top_keywords,
            "top_surfaced_memories": top_surfaced,
            "last_gc_ts": last_gc_ts,
            "last_gc_count": last_gc_count,
            "hook_calls": hook_calls,
            "total_entries": total_entries,
            "window_days": days,
        }

    def brief_stats(self) -> str:
        """Return a 1–3 line stats summary for injection into session-start context."""
        stats = self.aggregate_stats(days=7)

        total_memories = sum(stats["captures_by_layer"].values())
        project_cnt = stats["captures_by_layer"].get("project", 0)
        global_cnt = stats["captures_by_layer"].get("global", 0)
        captures_this_week = total_memories  # captures_by_layer already filters to 7d
        hook_calls = stats["hook_calls"]

        lines = [
            f"memories captured this week: {total_memories}"
            f" (project={project_cnt}, global={global_cnt})",
            f"hook searches this week: {hook_calls}",
        ]
        if stats["last_gc_ts"]:
            lines.append(
                f"last GC: {stats['last_gc_ts'][:10]}"
                f" (archived {stats['last_gc_count']})"
            )
        return " | ".join(lines)
