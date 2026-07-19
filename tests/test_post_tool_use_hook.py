"""Concurrency and latency contracts for the Claude PostToolUse hook."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "hooks" / "PostToolUse.sh"


def hook_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    fake_mnemos = bin_dir / "mnemos"
    fake_mnemos.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'run\\n' >> \"${MNEMOS_TEST_CALLS}\"\n"
        "sleep \"${MNEMOS_TEST_SLEEP:-1}\"\n"
        "printf '<mnemos-context type=\"background-activity\">done</mnemos-context>\\n'\n",
        encoding="utf-8",
    )
    fake_mnemos.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "MNEMOS_REPO_ROOT": str(tmp_path),
            "MNEMOS_BG_TS_FILE": str(tmp_path / "last-run.ts"),
            "MNEMOS_BG_LOCK_DIR": str(tmp_path / "worker.lock"),
            "MNEMOS_BG_RESULT_FILE": str(tmp_path / "result.txt"),
            "MNEMOS_BG_INTERVAL_MINUTES": "5",
            "MNEMOS_TEST_CALLS": str(calls),
            "MNEMOS_TEST_SLEEP": "1",
        }
    )
    return env, calls


def run_hook(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],
        input="{}",
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )


def wait_for(path: Path, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def wait_for_call_count(path: Path, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and len(path.read_text(encoding="utf-8").splitlines()) >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {count} calls in {path}")


def test_concurrent_hooks_launch_one_nonblocking_worker(tmp_path: Path) -> None:
    env, calls = hook_env(tmp_path)

    started = time.monotonic()
    processes = [
        subprocess.Popen(
            ["bash", str(HOOK)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(12)
    ]
    results = [process.communicate("{}", timeout=2) for process in processes]
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert all(process.returncode == 0 for process in processes)
    assert all(stderr == "" for _, stderr in results)

    wait_for(Path(env["MNEMOS_BG_RESULT_FILE"]))
    assert calls.read_text(encoding="utf-8").splitlines() == ["run"]

    delivered = run_hook(env)
    assert "background-activity" in delivered.stdout
    assert not Path(env["MNEMOS_BG_RESULT_FILE"]).exists()


def test_stale_lock_is_recovered(tmp_path: Path) -> None:
    env, calls = hook_env(tmp_path)
    lock_file = Path(env["MNEMOS_BG_LOCK_DIR"])
    lock_file.write_text("stale owner", encoding="utf-8")
    stale = time.time() - 60
    os.utime(lock_file, (stale, stale))

    result = run_hook(env)

    assert result.returncode == 0
    wait_for(Path(env["MNEMOS_BG_RESULT_FILE"]))
    assert calls.read_text(encoding="utf-8").splitlines() == ["run"]


def test_timed_out_worker_releases_lock_for_retry(tmp_path: Path) -> None:
    env, calls = hook_env(tmp_path)
    env.update(
        {
            "MNEMOS_BG_INTERVAL_MINUTES": "0",
            "MNEMOS_BG_WORKER_TIMEOUT_SECONDS": "1",
            "MNEMOS_TEST_SLEEP": "5",
        }
    )

    assert run_hook(env).returncode == 0
    wait_for_call_count(calls, 1)
    time.sleep(1.2)

    assert run_hook(env).returncode == 0
    wait_for_call_count(calls, 2)
