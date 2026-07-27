"""Regression coverage for bounded lifecycle-hook input readers."""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
READER_PATHS = [ROOT / "core" / "hook_input.py", ROOT / "hooks" / "hook_input.py"]


def load_reader(path: Path):
    spec = importlib.util.spec_from_file_location(f"hook_input_{path.parent.name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("reader_path", READER_PATHS)
def test_complete_json_returns_before_idle_window(
    reader_path: Path,
    monkeypatch,
) -> None:
    reader = load_reader(reader_path)
    read_fd, write_fd = os.pipe()
    payload = b'{"hook_event_name":"UserPromptSubmit","prompt":"hello"}'
    os.write(write_fd, payload)
    stream = os.fdopen(read_fd, "rb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stream)

    started = time.monotonic()
    try:
        result = reader.read_available_stdin()
    finally:
        stream.close()
        os.close(write_fd)
    elapsed = time.monotonic() - started

    assert result == payload
    assert elapsed < 0.05


@pytest.mark.parametrize("reader_path", READER_PATHS)
def test_fragmented_json_is_read_to_completion(
    reader_path: Path,
    monkeypatch,
) -> None:
    reader = load_reader(reader_path)
    read_fd, write_fd = os.pipe()
    payload = b'{"hook_event_name":"PostToolUse","tool_name":"Read"}'
    split_at = len(payload) - 4
    stream = os.fdopen(read_fd, "rb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stream)

    def write_fragments() -> None:
        os.write(write_fd, payload[:split_at])
        time.sleep(0.02)
        os.write(write_fd, payload[split_at:])

    writer = threading.Thread(target=write_fragments)
    writer.start()
    try:
        result = reader.read_available_stdin()
    finally:
        writer.join()
        stream.close()
        os.close(write_fd)

    assert result == payload


@pytest.mark.parametrize("reader_path", READER_PATHS)
def test_reader_stops_at_explicit_size_limit(
    reader_path: Path,
    monkeypatch,
) -> None:
    reader = load_reader(reader_path)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b'{"incomplete":"' + (b"x" * 128))
    stream = os.fdopen(read_fd, "rb", buffering=0)
    monkeypatch.setattr(sys, "stdin", stream)

    try:
        result = reader.read_available_stdin(max_bytes=64)
    finally:
        stream.close()
        os.close(write_fd)

    assert len(result) == 64
