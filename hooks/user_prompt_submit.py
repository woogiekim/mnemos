#!/usr/bin/env python3
"""Fast UserPromptSubmit hook entrypoint."""
from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hook_input import read_available_stdin


def _cache_paths(repo_root: str, session_id: str, prompt: str) -> tuple[Path, Path]:
    override = os.environ.get("MNEMOS_CONTEXT_CACHE_FILE")
    cache_dir = Path(
        os.environ.get("MNEMOS_CONTEXT_CACHE_DIR")
        or Path(os.environ.get("TMPDIR", "/tmp")) / "mnemos-context-cache"
    )
    session_key = hashlib.sha256(f"{repo_root}\0{session_id}".encode("utf-8")).hexdigest()
    prompt_key = hashlib.sha256(f"{repo_root}\0{session_id}\0{prompt}".encode("utf-8")).hexdigest()
    exact = Path(override) if override else cache_dir / "exact" / f"{prompt_key}.txt"
    last = cache_dir / "session" / f"{session_key}.txt"
    return exact, last


def _print_fresh_cache(exact: Path, last: Path, ttl: int) -> None:
    now = time.time()
    for path in (exact, last):
        try:
            if path.is_file() and (ttl < 0 or now - path.stat().st_mtime <= ttl):
                sys.stdout.write(path.read_text(encoding="utf-8"))
                return
        except OSError:
            continue


def _render_promotions(repo_root: str) -> None:
    obs_log = Path(repo_root) / ".agent" / "observability.jsonl"
    if not obs_log.exists():
        return

    cursor_path = Path(
        os.environ.get("MNEMOS_PROMO_CURSOR")
        or Path.home() / ".mnemos" / ".cache" / "promotion-cursor.txt"
    )
    max_bytes = int(os.environ.get("MNEMOS_PROMO_SCAN_MAX_BYTES", "262144"))
    cursor = {"ts": "2020-01-01T00:00:00Z", "offset": 0, "inode": None, "size": 0}
    if cursor_path.exists():
        raw_cursor = cursor_path.read_text(encoding="utf-8").strip()
        if raw_cursor.startswith("{"):
            try:
                cursor.update(json.loads(raw_cursor))
            except json.JSONDecodeError:
                pass
        elif raw_cursor:
            cursor["ts"] = raw_cursor

    stat = obs_log.stat()
    start = int(cursor.get("offset") or 0)
    if cursor.get("inode") not in (None, stat.st_ino) or start > stat.st_size:
        start = 0
    if start == 0 and stat.st_size > max_bytes:
        start = max(0, stat.st_size - max_bytes)

    with obs_log.open("rb") as fh:
        fh.seek(start)
        if start:
            fh.readline()
        data = fh.read(max_bytes + 1)
        end = fh.tell()
    if len(data) > max_bytes:
        data = data[:max_bytes]
        end = start + max_bytes

    promotions: list[str] = []
    for raw in data.decode("utf-8", errors="ignore").splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if entry.get("event") != "promotion":
            continue
        ts = str(entry.get("ts") or "")
        if ts <= str(cursor.get("ts") or ""):
            continue
        memory_id = entry.get("memory_id") or entry.get("item_id")
        layer = entry.get("layer") or entry.get("to_layer")
        if memory_id and layer:
            promotions.append(f"{memory_id} \u2192 {layer}")

    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        next_cursor = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "offset": end,
            "inode": stat.st_ino,
            "size": stat.st_size,
        }
        cursor_path.write_text(json.dumps(next_cursor, separators=(",", ":")) + "\n", encoding="utf-8")
    except OSError:
        pass

    if promotions:
        print("<mnemos-promotion>")
        for item in promotions:
            print(f"<promotion>{html.escape(item)}</promotion>")
        print("</mnemos-promotion>")


def _start_prefetch(
    *,
    script_dir: Path,
    source_root: Path,
    repo_root: str,
    session_id: str,
    prompt: str,
    exact: Path,
    last: Path,
) -> None:
    pythonpath = f"{source_root}{os.pathsep}{os.environ['PYTHONPATH']}" if os.environ.get("PYTHONPATH") else str(source_root)
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(script_dir / "context_prefetch_worker.py"),
                "--repo-root",
                repo_root,
                "--prompt",
                prompt,
                "--session-id",
                session_id,
                "--host",
                "claude-code",
                "--exact-cache",
                str(exact),
                "--last-cache",
                str(last),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": pythonpath},
            start_new_session=True,
        )
    except OSError:
        pass


def main() -> int:
    repo_root = os.environ.get("MNEMOS_REPO_ROOT", "")
    if not repo_root:
        return 0

    try:
        payload = json.loads(read_available_stdin() or b"{}")
    except json.JSONDecodeError:
        payload = {}
    session_id = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    if not prompt:
        return 0

    flag_dir = Path(os.environ.get("TMPDIR", "/tmp")) / "mnemos-session-flags"
    session_key = "".join(ch for ch in session_id if ch.isalnum() or ch in "_-")[:64]
    if session_key:
        try:
            flag_dir.mkdir(parents=True, exist_ok=True)
            (flag_dir / f"mnemos-session-loaded-{session_key}").touch()
        except OSError:
            pass

    if prompt == "/compact":
        print("[mnemos] /compact detected - manual fallback: capture key insights with mnemos capture before compacting.")
        return 0

    ttl = int(float(os.environ.get("MNEMOS_CONTEXT_CACHE_TTL_SECONDS", "300")))
    exact, last = _cache_paths(repo_root, session_id, prompt)
    _print_fresh_cache(exact, last, ttl)
    if os.environ.get("MNEMOS_RENDER_PROMOTIONS", "1") != "0":
        _render_promotions(repo_root)

    if os.environ.get("MNEMOS_CONTEXT_PREFETCH", "1") != "0":
        script_dir = Path(__file__).resolve().parent
        _start_prefetch(
            script_dir=script_dir,
            source_root=script_dir.parent,
            repo_root=repo_root,
            session_id=session_id,
            prompt=prompt,
            exact=exact,
            last=last,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
