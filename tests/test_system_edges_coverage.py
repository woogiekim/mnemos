"""Coverage for system-level edge paths in adapters, maintenance, and install tools."""
from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest


def test_claude_adapter_error_and_uninstall_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.adapters.claude import ClaudeCodeAdapter, _extract_repo_root_from_hook

    adapter = ClaudeCodeAdapter()
    settings = tmp_path / "settings.json"
    assert "skipped" in adapter._install_settings_json(settings, "/repo")[0]

    settings.write_text("{bad", encoding="utf-8")
    assert "warning" in adapter._install_settings_json(settings, "/repo")[0]
    with pytest.raises(RuntimeError):
        adapter._update_settings_json(settings)
    with pytest.raises(RuntimeError):
        adapter._remove_settings_json_hooks(settings)

    monkeypatch.setenv("MNEMOS_REPO_ROOT", "/env/repo")
    assert _extract_repo_root_from_hook({"hooks": [{"command": "echo noop"}]}) == "/env/repo"

    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{bad", encoding="utf-8")
    (claude_dir / "CLAUDE.md").write_text("plain", encoding="utf-8")
    ok, missing = adapter.verify_hooks(home)
    assert ok is False
    assert "settings.json (unreadable)" in missing
    assert "CLAUDE.md managed block" in missing

    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {"hooks": [{"command": "echo keep"}]},
                        {"hooks": [{"command": "mnemos capture --content x"}]},
                    ],
                    "UserPromptSubmit": [{"hooks": [{"command": "mnemos search x"}]}],
                },
                "other": True,
            }
        ),
        encoding="utf-8",
    )
    changed, diff = adapter._remove_settings_json_hooks(settings)
    assert changed is True
    assert "echo keep" in settings.read_text(encoding="utf-8")
    assert "mnemos search" in diff


def test_background_check_gc_and_memory_os_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.bg as bg

    class BadTimestamp:
        def exists(self) -> bool:
            return True

        def stat(self) -> Any:
            raise OSError("stat failed")

        def touch(self) -> None:
            raise OSError("touch failed")

    monkeypatch.setattr(bg, "_timestamp_path", lambda: BadTimestamp())
    assert bg.should_run() is True
    bg.touch_timestamp()

    group = bg.DuplicateGroup(
        content_hash="h",
        items=[
            {"item_id": "unknown", "layer": "custom"},
            {"item_id": "project", "layer": "project"},
        ],
    )
    assert group.primary["item_id"] == "project"

    class Store:
        def iter_layer_items(self, layer: str) -> list[dict[str, Any]]:
            return [
                {"id": "blank", "content": "  ", "_path": str(tmp_path / "blank.md")},
                {"id": "a", "content": "same", "_path": str(tmp_path / "a.md"), "layer": layer},
                {"id": "b", "content": " SAME ", "_path": str(tmp_path / "b.md"), "layer": layer},
            ]

    monkeypatch.setattr(bg, "MemoryStore", lambda repo_root: Store())
    assert len(bg.find_duplicates(str(tmp_path), layers=["project"])) == 1

    block = bg.BackgroundCheckResult(
        duplicate_groups=[group, group, group, group],
        memory_os_lifecycle_applied=1,
        memory_os_repaired=2,
        memory_os_retrieval_status="failed",
        memory_os_ready=False,
        memory_os_readiness_status="not_ready",
        memory_os_readiness_gaps=3,
        memory_os_validation_passed=False,
        memory_os_errors=["one", "two", "three"],
    ).to_context_block()
    assert "Memory OS applied" in block
    assert "... and 1 more" in block
    assert "maintenance error: two" in block

    monkeypatch.setattr(bg, "should_run", lambda interval_minutes: True)
    monkeypatch.setattr(bg, "touch_timestamp", lambda: None)
    monkeypatch.setattr(bg, "run_gc", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("gc")))
    monkeypatch.setattr(bg, "run_auto_promote", lambda repo_root: (_ for _ in ()).throw(RuntimeError("promote")))
    monkeypatch.setattr(bg, "find_duplicates", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("dedup")))
    result = bg.run_background_check(str(tmp_path), force=True)
    assert result.ran is True

    import core.gateway as gateway_mod
    import core.operations as operations_mod

    class Report:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class Engine:
        def __init__(self, gateway: Any) -> None:
            self.gateway = gateway

        def retrieval_backend_health(self) -> Report:
            return Report(status="degraded", partial_failure=True)

        def record_backend_health_report(self, report: Any, label: str) -> Path:
            return tmp_path / "backend.json"

        def recover_store(self, **kwargs: Any) -> Report:
            return Report(repaired_count=1, reindexed_count=2, corrupt_count=3)

        def record_recovery_report(self, report: Any, label: str) -> Path:
            return tmp_path / "recovery.json"

        def run_lifecycle(self, **kwargs: Any) -> Report:
            return Report(planned_count=2, applied_count=1)

        def record_lifecycle_report(self, report: Any, label: str) -> Path:
            return tmp_path / "lifecycle.json"

        def compute_metrics(self, **kwargs: Any) -> Report:
            return Report()

        def record_metrics_snapshot(self, report: Any, label: str) -> Path:
            return tmp_path / "metrics.json"

        def validate_health(self, **kwargs: Any) -> Report:
            return Report(status="failed", passed=False)

        def record_validation_report(self, report: Any, label: str) -> Path:
            return tmp_path / "validation.json"

        def audit_readiness(self, **kwargs: Any) -> Report:
            return Report(status="not_ready", ready=False, gaps=[1, 2])

        def record_readiness_report(self, report: Any, label: str) -> Path:
            return tmp_path / "readiness.json"

    monkeypatch.setattr(gateway_mod, "MemoryGateway", lambda repo_root: object())
    monkeypatch.setattr(operations_mod, "MemoryOperationsEngine", Engine)
    memory_os = bg.run_background_check(
        str(tmp_path),
        force=True,
        gc_enabled=False,
        auto_promote_enabled=False,
        dedup_enabled=False,
        memory_os_enabled=True,
        memory_os_recover=True,
        memory_os_apply=True,
    )
    assert memory_os.memory_os_lifecycle_applied == 1
    assert memory_os.memory_os_errors == []

    monkeypatch.setattr(operations_mod, "MemoryOperationsEngine", lambda gateway: (_ for _ in ()).throw(RuntimeError("memory os down")))
    failed = bg.run_background_check(
        str(tmp_path),
        force=True,
        gc_enabled=False,
        auto_promote_enabled=False,
        dedup_enabled=False,
        memory_os_enabled=True,
    )
    assert failed.memory_os_errors == ["memory os down"]


def test_gc_install_and_observability_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import install as install_mod
    from core.gc import (
        GCRegion,
        GCReport,
        GarbageCollector,
        MemoryRecord,
        _build_record,
        _parse_dt,
    )
    from core.observability import ObservabilityLogger

    record = MemoryRecord(
        item_id="item",
        layer="project",
        path=str(tmp_path / "item.md"),
        created_at=None,
        last_access_at=None,
        access_count=0,
        quality_score=0.1,
        stage="stored",
        content_preview="body",
        garbage_score=0.9,
        score_breakdown={},
    )
    assert GCRegion("project", [record]).collectable == [record]
    summary = GCReport(False, 1, 0, ["project"], [], [{"item_id": "item"}]).summary_lines()
    assert any("Skipped" in line for line in summary)
    assert _parse_dt(datetime.datetime(2026, 1, 1)).tzinfo is not None
    assert _parse_dt(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)).tzinfo is not None
    assert _parse_dt("2026-01-01T00:00:00").tzinfo is not None
    assert _parse_dt(object()) is None
    assert _build_record({"quality_score": object()}, "project", 1.0, datetime.datetime.now(datetime.timezone.utc)) is None

    collector = GarbageCollector.__new__(GarbageCollector)
    collector.gc_threshold = 0.1
    collector.limit = 1
    collector.scan = lambda: [GCRegion("project", [record])]

    class FailingStore:
        def update(self, path: str, metadata_updates: dict[str, Any]) -> None:
            raise RuntimeError("cannot archive")

    collector._store = FailingStore()
    report = collector.run(dry_run=False)
    assert report.skipped_items[0]["error"] == "cannot archive"

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("existing", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()

    class Adapter:
        def is_present(self, home_path: Path) -> bool:
            return False

    import core.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "ClaudeCodeAdapter", lambda: Adapter())
    monkeypatch.setattr(adapters_mod, "CursorAdapter", lambda: Adapter())
    install_mod.install(repo, home=home)
    assert "existing\n# mnemos" in (repo / ".gitignore").read_text(encoding="utf-8")

    zshrc = home / ".zshrc"
    zshrc.write_text("export PATH=/bin", encoding="utf-8")
    install_mod._install_zshrc(home, str(repo))
    assert "PATH=/bin\n# mnemos" in zshrc.read_text(encoding="utf-8")

    bad_root = tmp_path / "not-a-dir"
    bad_root.write_text("file", encoding="utf-8")
    ObservabilityLogger(str(bad_root))

    logger = ObservabilityLogger(str(tmp_path / "obs"))
    empty_logger = ObservabilityLogger(str(tmp_path / "empty-obs"))
    empty_logger._log_path.unlink()
    assert empty_logger.read_entries() == []
    captured: list[dict[str, Any]] = []
    monkeypatch.setattr(logger, "_write_async", lambda entry: captured.append(entry))
    logger.log_hook_post_tool("Edit", session_id="s", extra={"path": "x"})
    assert captured[0]["extra"] == {"path": "x"}

    original_open = Path.open

    def fail_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == logger._log_path:
            raise OSError("open failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_open)
    logger._write({"event": "x"})
    assert logger.read_entries() == []
    monkeypatch.setattr(Path, "open", original_open)

    logger._log_path.write_text(
        "\n".join(
            [
                "",
                "{bad",
                json.dumps({"event": "capture", "ts": "2026-05-25T00:00:00Z", "layer": "project", "session_id": "s"}),
                json.dumps({"event": "hook_session_start", "ts": "2026-05-25T00:00:00Z"}),
                json.dumps({"event": "gc", "ts": "2026-05-25T00:00:00Z", "archived_count": 4}),
            ]
        ),
        encoding="utf-8",
    )
    assert len(logger.read_entries(session_id="s", events=["capture"])) == 1
    assert "last GC" in logger.brief_stats()


def test_git_uninstaller_updater_and_misc_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.git as git_mod
    import core.uninstaller as uninstaller
    import core.updater as updater
    from core.gateway import _resolve_repo_root
    from core.transcript import _clean_lines

    original_subprocess_run = subprocess.run
    cwd_repo = tmp_path / "cwd-repo"
    (cwd_repo / "wiki").mkdir(parents=True)
    (cwd_repo / "wiki" / "policy.yaml").write_text("layers: {}\n", encoding="utf-8")
    monkeypatch.delenv("MNEMOS_REPO_ROOT", raising=False)
    monkeypatch.chdir(cwd_repo)
    assert _resolve_repo_root() == cwd_repo.resolve()

    import core.gateway as gateway_mod

    file_repo = tmp_path / "file-repo"
    (file_repo / "wiki").mkdir(parents=True)
    (file_repo / "wiki" / "policy.yaml").write_text("layers: {}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gateway_mod, "__file__", str(file_repo / "core" / "gateway.py"))
    assert _resolve_repo_root() == file_repo.resolve()

    assert _clean_lines("| table |")[1] == 1

    monkeypatch.setattr(git_mod, "_git", lambda *args, cwd: (1, "", "init failed"))
    with pytest.raises(git_mod.GitCommandError):
        git_mod.init(tmp_path)

    def set_url_error(*args: str, cwd: Path) -> tuple[int, str, str]:
        if args[:3] == ("remote", "get-url", "origin"):
            return 0, "old\n", ""
        return 2, "", "set-url failed"

    monkeypatch.setattr(git_mod, "_git", set_url_error)
    with pytest.raises(git_mod.GitCommandError):
        git_mod.set_remote(tmp_path, "origin", "new")

    def add_error(*args: str, cwd: Path) -> tuple[int, str, str]:
        if args[:3] == ("remote", "get-url", "origin"):
            return 1, "", "missing"
        return 3, "", "add failed"

    monkeypatch.setattr(git_mod, "_git", add_error)
    with pytest.raises(git_mod.GitCommandError):
        git_mod.set_remote(tmp_path, "origin", "url")

    def commit_noop(*args: str, cwd: Path) -> tuple[int, str, str]:
        if args[:3] == ("diff", "--cached", "--quiet"):
            return 1, "", ""
        if args[0] == "commit":
            return 1, "", "nothing"
        return 0, "", ""

    monkeypatch.setattr(git_mod, "_git", commit_noop)
    assert git_mod.commit(tmp_path, "msg") is False

    def commit_error(*args: str, cwd: Path) -> tuple[int, str, str]:
        if args[:3] == ("diff", "--cached", "--quiet"):
            return 1, "", ""
        if args[0] == "commit":
            return 2, "", "commit failed"
        return 0, "", ""

    monkeypatch.setattr(git_mod, "_git", commit_error)
    with pytest.raises(git_mod.GitCommandError):
        git_mod.commit(tmp_path, "msg")

    monkeypatch.setattr(git_mod, "_git", lambda *args, cwd: (4, "", "fetch failed"))
    with pytest.raises(git_mod.GitCommandError):
        git_mod.fetch(tmp_path, "origin")

    monkeypatch.setattr(git_mod.shutil, "which", lambda name: None)
    with pytest.raises(git_mod.GitNotFoundError):
        git_mod.rebase_continue(tmp_path)

    monkeypatch.setattr(git_mod, "current_branch", lambda path: (_ for _ in ()).throw(git_mod.GitCommandError(1, "branch failed")))

    def status_git(*args: str, cwd: Path) -> tuple[int, str, str]:
        if args[0] == "status":
            return 0, " M tracked\n?? new\n", ""
        return 1, "", ""

    monkeypatch.setattr(git_mod, "_git", status_git)
    status = git_mod.status(tmp_path)
    assert status["branch"] == ""
    assert status["dirty"] is True

    bad_settings = tmp_path / "settings.json"
    bad_settings.write_text("{bad", encoding="utf-8")
    with pytest.raises(RuntimeError):
        uninstaller.remove_settings_json_hooks(bad_settings)
    assert uninstaller._default_pipx_bin_dir().name == "bin"
    assert uninstaller._default_pipx_venvs_dir().name == "venvs"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "default-home")
    assert uninstaller._read_pipx_metadata() is None
    assert uninstaller._read_pipx_metadata(tmp_path / "missing") is None
    bad_meta_dir = tmp_path / "venvs" / "mnemos"
    bad_meta_dir.mkdir(parents=True)
    (bad_meta_dir / "pipx_metadata.json").write_text("{bad", encoding="utf-8")
    assert uninstaller._read_pipx_metadata(tmp_path / "venvs") is None

    run_calls: list[list[str]] = []
    monkeypatch.setattr(uninstaller.subprocess, "run", lambda args, **kwargs: run_calls.append(args))
    uninstaller.pipx_uninstall()
    assert run_calls == [["pipx", "uninstall", "mnemos"]]

    bin_dir = tmp_path / "bin"
    venvs_dir = tmp_path / "venvs-ok"
    bin_dir.mkdir()
    (bin_dir / "mnemos").write_text("bin", encoding="utf-8")
    (venvs_dir / "mnemos").mkdir(parents=True)
    (venvs_dir / "mnemos" / "pipx_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(uninstaller.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("pipx")))
    monkeypatch.setattr(uninstaller.shutil, "which", lambda name: "/tmp/mnemos")
    purge = uninstaller.purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)
    assert purge["binary_removed"] == bin_dir / "mnemos"
    assert purge["venv_removed"] == venvs_dir / "mnemos"
    assert purge["still_present"] is True

    (bin_dir / "mnemos").write_text("bin", encoding="utf-8")
    (venvs_dir / "mnemos").mkdir(parents=True)
    original_unlink = Path.unlink
    original_rmtree = shutil.rmtree

    def fail_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == bin_dir / "mnemos":
            raise OSError("cannot unlink")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(uninstaller.shutil, "rmtree", lambda path: (_ for _ in ()).throw(OSError("cannot rmtree")))
    purge_failed = uninstaller.purge_mnemos_artifacts(bin_dir=bin_dir, venvs_dir=venvs_dir)
    assert purge_failed["binary_removed"] is None
    assert purge_failed["venv_removed"] is None
    monkeypatch.setattr(Path, "unlink", original_unlink)
    monkeypatch.setattr(uninstaller.shutil, "rmtree", original_rmtree)

    default_home = tmp_path / "purge-home"
    (default_home / ".local" / "bin").mkdir(parents=True)
    (default_home / ".local" / "pipx" / "venvs").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: default_home)
    monkeypatch.setattr(uninstaller.shutil, "which", lambda name: None)
    assert uninstaller.purge_mnemos_artifacts()["still_present"] is False

    home = tmp_path / "uninstall-home"
    monkeypatch.setattr(Path, "home", lambda: home)
    assert uninstaller.run_uninstall(yes=True, home=None) == 0

    changed_home = tmp_path / "changed-home"
    changed_home.mkdir()
    (changed_home / ".zshrc").write_text("export MNEMOS_REPO_ROOT=/tmp\n", encoding="utf-8")

    class BrokenAdapter:
        name = "Broken"

        def uninstall(self, home_path: Path) -> list[str]:
            raise RuntimeError("adapter failed")

    import core.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod, "ClaudeCodeAdapter", lambda: BrokenAdapter())
    monkeypatch.setattr(adapters_mod, "CursorAdapter", lambda: BrokenAdapter())
    assert uninstaller.run_uninstall(yes=True, purge=False, home=changed_home) == 0

    purge_home = tmp_path / "purge-home-run"
    purge_home.mkdir()
    (purge_home / ".zshrc").write_text("export MNEMOS_REPO_ROOT=/tmp\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstaller,
        "purge_mnemos_artifacts",
        lambda: {
            "metadata": {},
            "pipx_ok": True,
            "binary_removed": tmp_path / "mnemos",
            "venv_removed": tmp_path / "venv",
            "still_present": False,
        },
    )
    assert uninstaller.run_uninstall(yes=True, purge=True, home=purge_home) == 0

    assert uninstaller._preview_settings_json(tmp_path / "no-settings.json") == ""
    assert uninstaller._preview_settings_json(bad_settings) == ""
    keep_settings = tmp_path / "keep-settings.json"
    keep_settings.write_text(
        json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "echo keep"}]}, {"hooks": [{"command": "mnemos search"}]}]}}),
        encoding="utf-8",
    )
    assert "echo keep" in uninstaller._preview_settings_json(keep_settings)
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export PATH=/bin\n", encoding="utf-8")
    assert uninstaller._preview_zshrc(zshrc) == ""
    zshrc.write_text("# mnemos root\nexport MNEMOS_REPO_ROOT=/tmp\n", encoding="utf-8")
    assert "mnemos root" in uninstaller._preview_zshrc(zshrc)
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "rules").write_text("plain rules", encoding="utf-8")
    assert uninstaller._preview_cursor_rules(cursor_dir) == ""

    monkeypatch.setattr(subprocess, "run", original_subprocess_run)
    assert updater._run(["true"]).returncode == 0
    updater_calls: list[list[str]] = []
    monkeypatch.setattr(updater, "_run", lambda cmd, cwd=None: updater_calls.append(cmd))
    updater.git_pull(str(tmp_path))
    updater.pipx_reinstall()
    assert ["git", "pull", "--rebase", "origin", "main"] in updater_calls
    assert ["pipx", "reinstall", "mnemos"] in updater_calls

    repo_root = tmp_path / "update-repo"
    (repo_root / "core").mkdir(parents=True)
    (repo_root / "agents").mkdir()
    assert updater.sync_source_to_install(str(repo_root), install_root=repo_root) == ["core", "agents"]

    original_resolve = Path.resolve

    def fail_core_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name == "core":
            raise OSError("resolve failed")
        return original_resolve(path, *args, **kwargs)

    copied: list[tuple[str, str]] = []
    monkeypatch.setattr(Path, "resolve", fail_core_resolve)
    monkeypatch.setattr(updater.shutil, "copytree", lambda src, dst, dirs_exist_ok: copied.append((src, dst)))
    assert "core" in updater.sync_source_to_install(str(repo_root), install_root=tmp_path / "install")
    monkeypatch.setattr(Path, "resolve", original_resolve)

    bad_update_settings = tmp_path / "bad-update-settings.json"
    bad_update_settings.write_text("{bad", encoding="utf-8")
    with pytest.raises(RuntimeError):
        updater.update_settings_json(bad_update_settings)

    import core.bg as bg_mod

    monkeypatch.delenv("MNEMOS_REPO_ROOT", raising=False)
    bg_calls: list[str] = []
    monkeypatch.setattr(bg_mod, "run_background_check", lambda repo_root, **kwargs: bg_calls.append(repo_root))
    updater._run_bg_check_quiet()
    assert bg_calls

    update_repo = tmp_path / "run-update-repo"
    (update_repo / "core").mkdir(parents=True)
    (update_repo / "agents").mkdir()
    update_home = tmp_path / "update-home"
    update_home.mkdir()

    class FailingUpdateAdapter:
        name = "Failing"

        def update(self, home_path: Path) -> list[str]:
            raise RuntimeError("update failed")

    monkeypatch.setattr(adapters_mod, "ClaudeCodeAdapter", lambda: FailingUpdateAdapter())
    monkeypatch.setattr(adapters_mod, "CursorAdapter", lambda: FailingUpdateAdapter())
    monkeypatch.setattr(updater, "pipx_reinstall", lambda: (_ for _ in ()).throw(subprocess.CalledProcessError(5, ["pipx"])))
    import core.install as install_mod

    monkeypatch.setattr(install_mod, "migrate_policy_transient", lambda repo_path: True)
    monkeypatch.setattr(updater, "_run_bg_check_quiet", lambda: None)
    assert updater.run_update(repo_root=str(update_repo), skip_git_pull=True, skip_pipx=False, home=update_home) == 1

    class QuietUpdateAdapter:
        name = "Quiet"

        def update(self, home_path: Path) -> list[str]:
            return []

    monkeypatch.setattr(adapters_mod, "ClaudeCodeAdapter", lambda: QuietUpdateAdapter())
    monkeypatch.setattr(adapters_mod, "CursorAdapter", lambda: QuietUpdateAdapter())
    monkeypatch.setattr(install_mod, "migrate_policy_transient", lambda repo_path: (_ for _ in ()).throw(RuntimeError("migration failed")))
    monkeypatch.delenv("MNEMOS_REPO_ROOT", raising=False)
    assert updater.run_update(repo_root=None, skip_git_pull=True, skip_pipx=True, home=update_home) == 0
