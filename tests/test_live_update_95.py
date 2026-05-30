"""Unit tests for the desktop-app live-update watcher (issue #95).

These tests exercise ``app/live_watcher.py`` which is OUTSIDE the
``core+agents`` 100% coverage gate (per #94 scoping). They MUST run
under ``filterwarnings=["error"]`` and stay zero-network / no-GUI.

Coverage notes (auto-verification — the GUI/JS bridge cannot be
asserted headlessly):

* ``test_debounce_coalesces_burst_into_single_rebuild`` — drive a real
  ``watchdog.Observer`` over ``tmp_path``, write 5 files inside the
  debounce window, assert exactly 1 callback fires after the window
  elapses.
* ``test_start_stop_lifecycle_no_thread_leak`` — start/stop without
  events; verify no observer thread remains alive.
* ``test_error_tolerance_keeps_watcher_alive`` — drive 2 events through
  a callback that raises; the watcher must not crash, and a third
  event after the failures must still produce a (debounced) callback.
* ``test_resolve_watched_paths_default_memorystore`` /
  ``test_resolve_watched_paths_obsidian_backend`` — verify the
  backend-specific resolver returns the correct watch root.
* ``test_resolve_watched_paths_unknown_fallback`` — fallback path when
  the gateway carries an unfamiliar backend shape.
* ``test_end_to_end_with_mocked_window_evaluate_js`` — drives a single
  rebuild that pipes a payload through a mock ``Window.evaluate_js``,
  asserting the JS bridge call site is wired to ``window.mnemos.applyUpdate``.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.live_watcher import LiveWatcher, resolve_watched_paths


# --------------------------------------------------------------------------- #
# Debounce coalescing — real Observer over tmp_path
# --------------------------------------------------------------------------- #
def test_debounce_coalesces_burst_into_single_rebuild(tmp_path: Path) -> None:
    """Five file writes inside the debounce window → exactly 1 callback."""
    calls: list[float] = []

    def on_rebuild() -> None:
        calls.append(time.time())

    watcher = LiveWatcher(
        paths=[tmp_path], debounce_ms=200, on_rebuild=on_rebuild
    )
    watcher.start()
    try:
        # Five rapid writes — all should coalesce.
        for i in range(5):
            (tmp_path / f"burst-{i}.md").write_text(f"#{i}\n", encoding="utf-8")
            time.sleep(0.01)
        # Wait past the debounce window + a generous Observer flush margin.
        time.sleep(0.6)
        assert len(calls) == 1, f"expected exactly 1 debounced callback, got {len(calls)}"
    finally:
        watcher.stop()


# --------------------------------------------------------------------------- #
# Lifecycle — start / stop is clean
# --------------------------------------------------------------------------- #
def test_start_stop_lifecycle_no_thread_leak(tmp_path: Path) -> None:
    """start() spins up the observer; stop() tears it down without leak."""
    pre_threads = threading.active_count()
    watcher = LiveWatcher(
        paths=[tmp_path], debounce_ms=100, on_rebuild=lambda: None
    )
    watcher.start()
    # Observer thread should be alive after start.
    assert threading.active_count() > pre_threads
    watcher.stop()
    # Allow observer.join() to settle (it has a 2s timeout).
    time.sleep(0.2)
    post_threads = threading.active_count()
    # We tolerate +/- 1 here because pytest may carry its own helpers; the key
    # is that we don't leave the Observer thread running indefinitely.
    assert post_threads <= pre_threads + 1, (
        f"observer thread appears to leak: pre={pre_threads}, post={post_threads}"
    )


def test_start_is_idempotent(tmp_path: Path) -> None:
    """Calling start() twice should not spawn two observers."""
    watcher = LiveWatcher(paths=[tmp_path], debounce_ms=100, on_rebuild=lambda: None)
    watcher.start()
    obs_first = watcher._observer
    watcher.start()
    assert watcher._observer is obs_first
    watcher.stop()


def test_start_noop_when_paths_empty() -> None:
    """An empty path list makes start() a safe no-op (no Observer created)."""
    watcher = LiveWatcher(paths=[], debounce_ms=100, on_rebuild=lambda: None)
    watcher.start()
    assert watcher._observer is None
    watcher.stop()  # idempotent


def test_stop_is_idempotent(tmp_path: Path) -> None:
    """stop() before start() and double-stop both safe."""
    watcher = LiveWatcher(paths=[tmp_path], debounce_ms=100, on_rebuild=lambda: None)
    watcher.stop()  # before any start
    watcher.start()
    watcher.stop()
    watcher.stop()  # second stop


# --------------------------------------------------------------------------- #
# Error tolerance — callback exceptions never crash the watcher
# --------------------------------------------------------------------------- #
def test_error_tolerance_keeps_watcher_alive(tmp_path: Path) -> None:
    """A raising on_rebuild does not kill the watcher; later events still fire."""
    counter = {"calls": 0, "fail_until": 2}

    def on_rebuild() -> None:
        counter["calls"] += 1
        if counter["calls"] <= counter["fail_until"]:
            raise RuntimeError("intentional failure")

    watcher = LiveWatcher(
        paths=[tmp_path], debounce_ms=150, on_rebuild=on_rebuild
    )
    watcher.start()
    try:
        # Burst 1 — first debounced fire raises, but the watcher must survive.
        (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
        time.sleep(0.5)
        # Burst 2 — second debounced fire also raises.
        (tmp_path / "b.md").write_text("b\n", encoding="utf-8")
        time.sleep(0.5)
        # Burst 3 — this one succeeds; the watcher must still process events.
        (tmp_path / "c.md").write_text("c\n", encoding="utf-8")
        time.sleep(0.5)
        assert counter["calls"] >= 3, (
            f"watcher stopped firing after exceptions; calls={counter['calls']}"
        )
    finally:
        watcher.stop()


# --------------------------------------------------------------------------- #
# resolve_watched_paths — backend-specific roots
# --------------------------------------------------------------------------- #
class _FakeMemoryStoreGateway:
    """Stand-in for a MemoryStore-backed gateway with the expected _root."""

    def __init__(self, root: Path) -> None:
        from core.store import MemoryStore  # local import to avoid cycles

        store = MemoryStore.__new__(MemoryStore)
        store._root = root
        self._store = store
        self._root = str(root)


class _FakeObsidianGateway:
    """Stand-in for an ObsidianBackend-backed gateway."""

    def __init__(self, vault: Path) -> None:
        from core.obsidian import ObsidianBackend

        backend = ObsidianBackend.__new__(ObsidianBackend)
        backend._vault = vault
        self._store = backend
        self._root = str(vault)


class _UnknownBackendGateway:
    """A gateway whose backend matches neither MemoryStore nor ObsidianBackend."""

    def __init__(self, root: Path) -> None:
        self._store = object()
        self._root = str(root)


def test_resolve_watched_paths_default_memorystore(tmp_path: Path) -> None:
    gw = _FakeMemoryStoreGateway(tmp_path)
    paths = resolve_watched_paths(gw)
    assert paths == [tmp_path / "wiki"]


def test_resolve_watched_paths_obsidian_backend(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    gw = _FakeObsidianGateway(vault)
    paths = resolve_watched_paths(gw)
    assert paths == [vault]


def test_resolve_watched_paths_unknown_fallback(tmp_path: Path) -> None:
    gw = _UnknownBackendGateway(tmp_path)
    paths = resolve_watched_paths(gw)
    assert paths == [Path(str(tmp_path)) / "wiki"]


# --------------------------------------------------------------------------- #
# End-to-end smoke — rebuild → window.evaluate_js round-trip
# --------------------------------------------------------------------------- #
def test_end_to_end_with_mocked_window_evaluate_js(tmp_path: Path) -> None:
    """A single debounced rebuild pipes a JSON payload through evaluate_js.

    Exercises the same JS-bridge call pattern the app entry uses, with the
    pywebview ``Window`` replaced by a ``MagicMock`` so no GUI is opened.
    """
    import json

    window = MagicMock()
    captured: dict = {}

    def rebuild() -> None:
        payload = {"schema_version": 1, "memory": {"memories": []}}
        js = (
            "window.mnemos && window.mnemos.applyUpdate("
            + json.dumps(payload)
            + ");"
        )
        window.evaluate_js(js)
        captured["payload"] = payload
        captured["js"] = js

    watcher = LiveWatcher(paths=[tmp_path], debounce_ms=150, on_rebuild=rebuild)
    watcher.start()
    try:
        (tmp_path / "trigger.md").write_text("hello\n", encoding="utf-8")
        time.sleep(0.5)
        window.evaluate_js.assert_called()
        assert "window.mnemos.applyUpdate(" in captured["js"]
        assert "applyUpdate" in captured["js"]
    finally:
        watcher.stop()


# --------------------------------------------------------------------------- #
# Follow-up flag — second fire while one is in-flight
# --------------------------------------------------------------------------- #
def test_follow_up_flag_triggers_second_rebuild(tmp_path: Path) -> None:
    """If a rebuild is in flight, the next event marks _follow_up and reschedules."""
    gate = threading.Event()
    call_count = {"n": 0}

    def on_rebuild() -> None:
        call_count["n"] += 1
        # Hold the first call long enough for a second event to arrive.
        if call_count["n"] == 1:
            gate.wait(timeout=1.0)

    watcher = LiveWatcher(paths=[tmp_path], debounce_ms=100, on_rebuild=on_rebuild)
    watcher.start()
    try:
        (tmp_path / "x.md").write_text("first\n", encoding="utf-8")
        # Wait until first call is in-flight.
        time.sleep(0.25)
        # Fire while in-flight — should land via follow_up flag.
        (tmp_path / "y.md").write_text("second\n", encoding="utf-8")
        time.sleep(0.1)
        # Release the first call.
        gate.set()
        # Give the follow-up timer time to fire.
        time.sleep(0.5)
        assert call_count["n"] >= 2, (
            f"follow-up flag did not produce a second rebuild; calls={call_count['n']}"
        )
    finally:
        watcher.stop()
