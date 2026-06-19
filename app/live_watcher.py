"""Long-running file-system watcher for the desktop app (issue #95).

This module lives OUTSIDE ``core/`` + ``agents/`` so it is NOT part of
the 100% coverage gate (per #94's coverage scope). It IS subject to
``filterwarnings=["error"]`` and is exercised by the dedicated
``tests/test_live_update_95.py`` suite with a real ``watchdog.Observer``
over a ``tmp_path``.

Responsibilities:

1. Subscribe to file-system events under the memory-store root(s) the
   gateway reads from (default ``MemoryStore``: ``<repo>/wiki``;
   :class:`~core.obsidian.ObsidianBackend`: ``<vault>``; fallback:
   ``<gw._root>/wiki``).
2. Coalesce bursty events with a single :class:`threading.Timer` re-armed
   on every event so a bulk import or a multi-file capture fires at most
   one rebuild per debounce window.
3. Serialize callbacks — if a rebuild is already running, mark a
   "follow-up" flag and let the in-flight rebuild re-arm the timer when
   it finishes so the latest payload always wins without overlapping
   pushes through the pywebview JS bridge.
4. Tolerate ``on_rebuild`` exceptions silently (``except Exception``) so a
   transient store error never crashes the long-running app process; log
   via :mod:`core.observability` when importable.

The watcher is the Python half of the live-update mechanism documented
in issue #95. The JS half (``window.mnemos.applyUpdate``) lives in
``core/templates/ui.html``; this module never imports it.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable


class LiveWatcher:
    """Debounced, re-entrancy-safe filesystem watcher.

    Args:
        paths: List of directories to monitor recursively. Empty list is
            tolerated — :meth:`start` becomes a no-op so the caller can
            uniformly handle "no watched paths" (e.g. an unknown backend
            shape) without branching.
        debounce_ms: Coalescing window in milliseconds. Events arriving
            within this window after the first event are merged into a
            single rebuild.
        on_rebuild: Zero-arg callable invoked once per debounced window.
            Exceptions are caught and logged (never re-raised).
    """

    def __init__(
        self,
        *,
        paths: list[Path],
        debounce_ms: int,
        on_rebuild: Callable[[], None],
    ) -> None:
        self._paths = list(paths)
        self._debounce_s = max(0.001, float(debounce_ms) / 1000.0)
        self._on_rebuild = on_rebuild

        # Re-entrancy state.
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._in_flight = False
        self._follow_up = False

        # Observer is created lazily so a watcher constructed but never
        # started never imports watchdog (lets tests that only exercise
        # ``resolve_watched_paths`` run without the optional dep).
        self._observer: Any = None

    # ------------------------------------------------------------------ #
    # Public lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Begin watching the configured paths.

        No-op when ``paths`` is empty. Idempotent — calling ``start``
        twice on the same instance starts a single Observer; the second
        call is a no-op.
        """
        if not self._paths or self._observer is not None:
            return

        # Lazy import — keeps non-live test paths (e.g. config tests)
        # importable without the ``[ui]`` extra installed. If watchdog is not
        # installed, live updates degrade to a no-op instead of crashing app
        # launch.
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ModuleNotFoundError as exc:
            if exc.name == "watchdog":
                return
            raise

        watcher = self  # captured for the inner class

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:  # noqa: D401
                """Re-arm the debounce timer on every fs event."""
                watcher._arm_timer()

        self._observer = Observer()
        handler = _Handler()
        for p in self._paths:
            # Watchdog accepts strings only — Path → str at the boundary.
            self._observer.schedule(handler, str(p), recursive=True)
        self._observer.start()

    def stop(self) -> None:
        """Stop the observer and cancel any pending debounce timer.

        Idempotent — calling ``stop`` before ``start`` or after a prior
        ``stop`` is safe. Used both by manual teardown and by
        ``atexit`` so the watcher never lingers past process exit.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                # Defensive — never let teardown raise.
                pass
            self._observer = None

    # ------------------------------------------------------------------ #
    # Internal — debounce + serialization
    # ------------------------------------------------------------------ #
    def _arm_timer(self) -> None:
        """Cancel + re-arm the single debounce timer."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_s, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        """Invoke ``on_rebuild`` exactly once, serialized via a lock.

        When called while a previous rebuild is still in flight, set the
        ``_follow_up`` flag and return. The in-flight rebuild re-arms
        the timer when it finishes, so the latest payload always wins.
        """
        with self._lock:
            if self._in_flight:
                self._follow_up = True
                return
            self._in_flight = True

        try:
            self._on_rebuild()
        except Exception:  # pragma: no cover - broad-except for safety
            # Log via observability if importable; otherwise silent.
            try:
                from core import observability

                observability.log_event(
                    "live_watcher_rebuild_error", level="warning"
                )
            except Exception:
                pass
        finally:
            with self._lock:
                self._in_flight = False
                follow_up = self._follow_up
                self._follow_up = False
            if follow_up:
                # Re-arm so the latest fs activity gets one more debounced
                # rebuild after the in-flight one completes.
                self._arm_timer()


def resolve_watched_paths(gateway: Any) -> list[Path]:
    """Return the directories to watch for *gateway*.

    Inspects the gateway's storage backend and returns the
    backend-specific root(s):

    * Default :class:`~core.store.MemoryStore` → ``[<repo_root>/wiki]``.
    * :class:`~core.obsidian.ObsidianBackend` → ``[<vault_path>]``.
    * Anything else → ``[<gw._root>/wiki]`` (best-effort fallback so an
      unknown backend still surfaces a reasonable watched root rather
      than silently watching nothing).

    The returned paths are always non-empty for known backends; callers
    pass the list straight into :class:`LiveWatcher`.
    """
    # Default MemoryStore — watch the wiki/ tree under the repo root.
    try:
        from core.store import MemoryStore

        if isinstance(gateway._store, MemoryStore):
            return [gateway._store._root / "wiki"]
    except Exception:
        pass

    # Obsidian backend — watch the vault directory.
    try:
        from core.obsidian import ObsidianBackend

        if isinstance(gateway._store, ObsidianBackend):
            return [gateway._store._vault]
    except Exception:
        pass

    # Fallback — derive from the gateway's known repo root.
    return [Path(gateway._root) / "wiki"]


__all__ = ["LiveWatcher", "resolve_watched_paths"]
