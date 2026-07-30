"""Capture CLI behavior when git-backed remote sync partially fails."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from tests.test_store_sync import _git, _init_repo_with_main, _make_bare


def _write_sync_config(repo: Path, *, auto_pull_on_capture: bool = False) -> None:
    for layer_dir in ["global", "projects", "entities", "claims", "topics"]:
        (repo / "wiki" / layer_dir).mkdir(parents=True, exist_ok=True)
    (repo / "wiki" / "policy.yaml").write_text(
        "\n".join(
            [
                "layers:",
                "  project:",
                "    path_template: wiki/projects/",
                "    promotes_to: global",
                "    promotion: {age_hours: 0, access_count: 999, quality_score: 1}",
                "  global:",
                "    path_template: wiki/global/",
                "    promotes_to: null",
                "    promotion: {age_hours: 0, access_count: 999, quality_score: 1}",
                "forget:",
                "  requires_archived: true",
                "archive:",
                "  allowed_stages: [stored, retrieved, used, validated]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    repo.joinpath("mnemos.yml").write_text(
        "\n".join(
            [
                "storage:",
                "  sync:",
                "    enabled: true",
                "    remote: origin",
                "    branch: main",
                "    mode: auto",
                f"    auto_pull_on_capture: {str(auto_pull_on_capture).lower()}",
                "    auto_push_after_commit: true",
                "    pull_rate_limit_seconds: 0",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _remote_subjects(bare: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--pretty=%s", "main"],
        cwd=str(bare),
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def test_capture_json_reports_committed_capture_when_push_ref_race_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A non-fast-forward push must not make a committed capture look lost."""
    bare = _make_bare(tmp_path / "memory.git")
    repo = tmp_path / "memory-a"
    _init_repo_with_main(repo, bare=bare)
    _write_sync_config(repo, auto_pull_on_capture=False)

    before_count = _commit_count(repo)

    other = tmp_path / "memory-b"
    subprocess.run(
        ["git", "clone", str(bare), str(other)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git("config", "user.email", "test@mnemos.test", cwd=other)
    _git("config", "user.name", "mnemos Test", cwd=other)
    _git("config", "commit.gpgsign", "false", cwd=other)
    (other / "wiki" / "projects").mkdir(parents=True, exist_ok=True)
    (other / "wiki" / "projects" / "remote-first.md").write_text(
        "---\nid: remote-first\nlayer: project\n---\nremote moved first\n",
        encoding="utf-8",
    )
    _git("add", "wiki/projects/remote-first.md", cwd=other)
    _git("commit", "-m", "remote moves first", cwd=other)
    _git("push", "origin", "main", cwd=other)

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo))
    from core.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "capture",
            "--json",
            "--layer",
            "project",
            "--id",
            "local-race",
            "--content",
            "local capture survives push race",
            "--no-classify",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "sync_pending"
    assert payload["capture_status"] == "committed"
    assert payload["sync_status"] == "failed"
    assert payload["retryable"] is True
    assert payload["error_code"] in {"remote_ref_mismatch", "non_fast_forward"}
    assert payload["id"] == "local-race"
    assert payload["commit"] == _head(repo)
    assert payload["recovery_command"] == "mnemos sync pull && mnemos sync push"
    assert "git push" in payload["stderr"]

    assert _commit_count(repo) == before_count + 1
    assert (repo / "wiki" / "projects" / "local-race.md").exists()
    assert not any("local-race" in subject for subject in _remote_subjects(bare))


def test_capture_text_reports_sync_pending_after_push_ref_race(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Human output must say the capture is local and remote sync is pending."""
    bare = _make_bare(tmp_path / "memory.git")
    repo = tmp_path / "memory-a"
    _init_repo_with_main(repo, bare=bare)
    _write_sync_config(repo, auto_pull_on_capture=False)

    other = tmp_path / "memory-b"
    subprocess.run(
        ["git", "clone", str(bare), str(other)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git("config", "user.email", "test@mnemos.test", cwd=other)
    _git("config", "user.name", "mnemos Test", cwd=other)
    _git("config", "commit.gpgsign", "false", cwd=other)
    (other / "wiki" / "global").mkdir(parents=True, exist_ok=True)
    (other / "wiki" / "global" / "remote-first.md").write_text(
        "---\nid: remote-text-first\nlayer: global\n---\nremote moved first\n",
        encoding="utf-8",
    )
    _git("add", "wiki/global/remote-first.md", cwd=other)
    _git("commit", "-m", "remote text moves first", cwd=other)
    _git("push", "origin", "main", cwd=other)

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo))
    from core.cli import cli

    result = CliRunner(mix_stderr=False).invoke(
        cli,
        [
            "capture",
            "--layer",
            "project",
            "--id",
            "local-text-race",
            "--content",
            "local text capture survives push race",
            "--no-color",
            "--no-classify",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "captured: local-text-race" in result.stdout
    assert "remote sync pending" in result.stderr
    assert "mnemos sync pull && mnemos sync push" in result.stderr
    assert "success" not in result.stderr.lower()
    assert (repo / "wiki" / "projects" / "local-text-race.md").exists()


def test_capture_json_reports_retryable_index_lock_without_claiming_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """An existing git index.lock is a retryable commit failure, not a push failure."""
    bare = _make_bare(tmp_path / "memory.git")
    repo = tmp_path / "memory-lock"
    _init_repo_with_main(repo, bare=bare)
    _write_sync_config(repo, auto_pull_on_capture=False)
    lock_path = repo / ".git" / "index.lock"
    lock_path.write_text("held by another git process\n", encoding="utf-8")

    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(repo))
    from core.cli import cli

    result = CliRunner().invoke(
        cli,
        [
            "capture",
            "--json",
            "--layer",
            "project",
            "--id",
            "lock-case",
            "--content",
            "lock should not be reported as committed",
            "--no-classify",
        ],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "error"
    assert payload["capture_status"] == "written_uncommitted"
    assert payload["sync_status"] == "commit_failed"
    assert payload["retryable"] is True
    assert payload["error_code"] == "git_index_locked"
    assert payload["id"] == "lock-case"
    assert payload["commit"] is None
    assert "index.lock" in payload["stderr"]
    assert lock_path.exists(), "mnemos must not remove index.lock without safe ownership proof"
    assert (repo / "wiki" / "projects" / "lock-case.md").exists()
