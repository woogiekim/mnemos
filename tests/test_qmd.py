"""Tests for the optional local QMD retrieval adapter."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


_FAKE_QMD_COMPLETION_TIMEOUT_SECONDS = 5.0


def _write_fake_qmd(path: Path) -> Path:
    script = """#!/usr/bin/env python3
import json
import os
import sys
import time

log_path = os.environ.get("FAKE_QMD_ARGV_LOG")
if log_path:
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump({"argv": sys.argv[1:]}, handle)

calls_path = os.environ.get("FAKE_QMD_CALLS_LOG")
if calls_path:
    with open(calls_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:]}) + "\\n")

sleep_seconds = float(os.environ.get("FAKE_QMD_SLEEP", "0"))
if sleep_seconds:
    time.sleep(sleep_seconds)

exit_code = int(os.environ.get("FAKE_QMD_EXIT", "0"))
if exit_code:
    print("fake qmd failed", file=sys.stderr)
    raise SystemExit(exit_code)

print(os.environ.get("FAKE_QMD_OUTPUT", "[]"))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


class _FileStore:
    def __init__(self, items: dict[Path, dict[str, Any]]) -> None:
        self._items = {path.resolve(): dict(item) for path, item in items.items()}

    def parse_file(self, path: Path) -> dict[str, Any]:
        return dict(self._items[path.resolve()])

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        candidate = Path(item_id_or_path).expanduser()
        if candidate.is_absolute() and candidate.resolve() in self._items:
            return dict(self._items[candidate.resolve()])
        for item in self._items.values():
            if str(item.get("id")) == item_id_or_path:
                return dict(item)
        raise FileNotFoundError(item_id_or_path)


def test_success_case_qmd_search_uses_argv_json_and_maps_stable_memory_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-101/104: shell interpolation or unstable path IDs must break this test."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    argv_log = tmp_path / "argv.json"
    memory_path = tmp_path / "vault" / "project" / "decision.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("canonical", encoding="utf-8")
    query = "same meaning; touch /tmp/never"
    monkeypatch.setenv("FAKE_QMD_ARGV_LOG", str(argv_log))
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps([
            {
                "docid": "#abc123",
                "score": 0.91,
                "file": str(memory_path),
                "title": "Decision",
                "snippet": "semantic result",
                "line": 4,
            }
        ]),
    )
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        index_name="mnemos-test",
        mode="query",
        timeout_seconds=3.0,
        model_ready=True,
    )
    store = _FileStore({
        memory_path: {
            "id": "stable-memory-id",
            "content": "canonical content",
            "layer": "project",
            "quality_score": 0.9,
            "_path": str(memory_path),
        }
    })
    sut = QmdAdapter(repo_root=tmp_path, store=store, config=config)
    sut.prepare_index_config({"mnemos-project": memory_path.parent})

    # when
    results = sut.search(query, limit=7)

    # then
    assert results == [
        {
            "item_id": "stable-memory-id",
            "content": "canonical content",
            "metadata": {
                "id": "stable-memory-id",
                "layer": "project",
                "quality_score": 0.9,
                "qmd_docid": "#abc123",
                "qmd_file": str(memory_path),
                "qmd_line": 4,
            },
            "score": 0.91,
            "semantic_score": 0.91,
            "source": "qmd",
        }
    ]
    argv = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
    assert argv == [
        "--index",
        "mnemos-test",
        "query",
        "--format",
        "json",
        "--full-path",
        "-n",
        "7",
        query,
    ]
    assert sut.diagnostics()["status"] == "available"


def test_success_case_qmd_bm25_scores_are_monotonic_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QMD keyword scores above one must retain their relevance ordering."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    collection = tmp_path / "vault" / "project"
    paths = [collection / "strong.md", collection / "moderate.md"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("canonical", encoding="utf-8")
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps(
            [
                {"file": str(paths[0]), "score": 25.0},
                {"file": str(paths[1]), "score": 5.0},
            ]
        ),
    )
    store = _FileStore(
        {
            path: {"id": path.stem, "content": path.stem, "layer": "project"}
            for path in paths
        }
    )
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=store,
        config=QmdConfig(enabled=True, executable=str(executable), mode="search"),
    )
    sut.prepare_index_config({"mnemos-project": collection})

    # when
    results = sut.search("keyword", limit=2)

    # then
    assert [result["item_id"] for result in results] == ["strong", "moderate"]
    assert results[0]["semantic_score"] == pytest.approx(25.0 / 26.0)
    assert results[1]["semantic_score"] == pytest.approx(5.0 / 6.0)
    assert results[0]["semantic_score"] > results[1]["semantic_score"]


def test_failure_case_qmd_missing_binary_returns_explicit_degraded_diagnostics(
    tmp_path: Path,
) -> None:
    """TC-102: a missing optional binary must not raise or erase fallback results."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    config = QmdConfig(
        enabled=True,
        executable=str(tmp_path / "missing-qmd"),
        timeout_seconds=0.1,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)
    sut.prepare_index_config({"mnemos-project": tmp_path / "vault"})

    # when
    results = sut.search("query", limit=5)

    # then
    assert results == []
    diagnostics = sut.diagnostics()
    assert diagnostics["status"] == "missing"
    assert diagnostics["configured"] is True
    assert diagnostics["available"] is False
    assert diagnostics["degraded"] is True
    assert "query" not in json.dumps(diagnostics)


def test_failure_case_qmd_initial_health_reports_missing_index_configuration(
    tmp_path: Path,
) -> None:
    """Health checks must expose setup drift before the first recall call."""
    from core.config import QmdConfig
    from core.vector import VectorBackend

    # given
    config = QmdConfig(enabled=True)
    sut = VectorBackend(
        repo_root=tmp_path,
        store=_FileStore({}),
        qmd_config=config,
    )

    # when
    diagnostics = sut.diagnostics()

    # then
    assert diagnostics["status"] == "not_configured"
    assert diagnostics["available"] is False
    assert diagnostics["degraded"] is True
    assert diagnostics["reason"] == "repo-local qmd index config is missing"


def test_boundary_case_disabled_qmd_search_and_update_never_run_a_process(
    tmp_path: Path,
) -> None:
    """Disabled QMD must remain inert for both read and refresh paths."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter, QmdCommandError

    # given
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=False),
    )

    # when
    results = sut.search("disabled query")
    with pytest.raises(QmdCommandError) as captured:
        sut.update_index({"mnemos-project": tmp_path / "vault"})

    # then
    assert results == []
    assert sut.diagnostics()["status"] == "disabled"
    assert captured.value.code == "disabled"
    assert not (tmp_path / ".agent" / "state" / "qmd").exists()


def test_failure_case_unprepared_qmd_search_reports_missing_local_config(
    tmp_path: Path,
) -> None:
    """Recall must degrade clearly when qmd-prepare has not been run."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True),
    )

    # when
    results = sut.search("unprepared query")

    # then
    assert results == []
    assert sut.diagnostics()["status"] == "not_configured"


def test_boundary_case_invalid_direct_mode_falls_back_to_model_free_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bypassed config parser must never default to a model-backed command."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    argv_log = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_QMD_ARGV_LOG", str(argv_log))
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        index_name="../escape",
        mode="invalid",
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)
    sut.prepare_index_config({"mnemos-project": tmp_path / "vault"})

    # when
    results = sut.search("모델 없는 검색", limit=5)

    # then
    assert results == []
    argv = json.loads(argv_log.read_text(encoding="utf-8"))["argv"]
    assert argv[:2] == ["--index", "mnemos"]
    assert argv[2] == "search"
    assert (
        tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
    ).is_file()
    assert not (tmp_path / ".agent" / "state" / "qmd" / "escape.yml").exists()


def test_failure_case_qmd_process_os_error_is_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch-time OS error must degrade without exposing the query."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True, executable=str(executable)),
    )
    sut.prepare_index_config({"mnemos-project": tmp_path / "vault"})
    monkeypatch.setattr(
        "core.qmd.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private query")),
    )

    # when
    results = sut.search("private query")

    # then
    assert results == []
    assert sut.diagnostics()["status"] == "error"
    assert sut.diagnostics()["reason"] == "OSError"
    assert "private query" not in json.dumps(sut.diagnostics())


def test_boundary_case_qmd_executable_resolution_does_not_trust_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare commands use PATH; explicit relative paths are rooted in the repo."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    trusted_dir = tmp_path / "trusted-bin"
    trusted_dir.mkdir()
    trusted = _write_fake_qmd(trusted_dir / "qmd")
    malicious_cwd = tmp_path / "untrusted-cwd"
    malicious_cwd.mkdir()
    _write_fake_qmd(malicious_cwd / "qmd")
    repo_dot_relative = _write_fake_qmd(tmp_path / "qmd")
    (tmp_path / "tools").mkdir()
    repo_relative = _write_fake_qmd(tmp_path / "tools" / "qmd")
    monkeypatch.setenv("PATH", str(trusted_dir))
    monkeypatch.chdir(malicious_cwd)
    bare = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True, executable="qmd"),
    )
    explicit = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True, executable="tools/qmd"),
    )
    explicit_dot = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True, executable="./qmd"),
    )

    # when
    bare_path = bare._resolve_executable()
    explicit_path = explicit._resolve_executable()
    explicit_dot_path = explicit_dot._resolve_executable()

    # then
    assert bare_path == str(trusted.resolve())
    assert explicit_path == str(repo_relative.resolve())
    assert explicit_dot_path == str(repo_dot_relative.resolve())
    assert bare_path != str((malicious_cwd / "qmd").resolve())


@pytest.mark.parametrize(
    ("executable_kind", "raised", "expected_code"),
    [
        ("missing", None, "missing_executable"),
        ("present", "timeout", "timeout"),
        ("present", "os_error", "process_error"),
    ],
)
def test_failure_case_qmd_index_update_normalizes_process_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_kind: str,
    raised: str | None,
    expected_code: str,
) -> None:
    """Refresh failures expose bounded codes instead of process details."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter, QmdCommandError

    # given
    executable = tmp_path / "missing-qmd"
    if executable_kind == "present":
        executable = _write_fake_qmd(tmp_path / "fake-qmd")
    if raised == "timeout":
        monkeypatch.setattr(
            "core.qmd.subprocess.run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("qmd", 1)
            ),
        )
    elif raised == "os_error":
        monkeypatch.setattr(
            "core.qmd.subprocess.run",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private")),
        )
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({}),
        config=QmdConfig(enabled=True, executable=str(executable)),
    )

    # when
    with pytest.raises(QmdCommandError) as captured:
        sut.update_index({"mnemos-project": tmp_path / "vault"})

    # then
    assert captured.value.code == expected_code
    assert "private" not in str(captured.value)


@pytest.mark.parametrize(
    ("environment", "timeout_seconds", "expected_status"),
    [
        ({"FAKE_QMD_SLEEP": "1.0"}, 0.05, "timeout"),
        (
            {"FAKE_QMD_OUTPUT": "not-json"},
            _FAKE_QMD_COMPLETION_TIMEOUT_SECONDS,
            "invalid_output",
        ),
        (
            {"FAKE_QMD_EXIT": "3"},
            _FAKE_QMD_COMPLETION_TIMEOUT_SECONDS,
            "error",
        ),
    ],
)
def test_failure_case_qmd_process_failures_are_bounded_and_content_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
    timeout_seconds: float,
    expected_status: str,
) -> None:
    """TC-103: timeout, malformed output, and non-zero exit must degrade safely."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        timeout_seconds=timeout_seconds,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)
    sut.prepare_index_config({"mnemos-project": tmp_path / "vault"})

    # when
    results = sut.search("private failure query", limit=5)

    # then
    assert results == []
    diagnostics = sut.diagnostics()
    assert diagnostics["status"] == expected_status
    assert diagnostics["degraded"] is True
    assert "private failure query" not in json.dumps(diagnostics)


def test_success_case_qmd_config_resolves_yaml_with_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-105: QMD is opt-in and environment settings override repo config."""
    from core.config import QmdConfig, get_qmd_config

    # given
    (tmp_path / "mnemos.yml").write_text(
        yaml.safe_dump(
            {
                "retrieval": {
                    "qmd": {
                        "enabled": True,
                        "executable": "/opt/qmd",
                        "index_name": "repo-index",
                        "mode": "vsearch",
                        "timeout_seconds": 9,
                        "update_timeout_seconds": 75,
                        "embed_on_update": True,
                        "embed_model": "hf:Qwen/Qwen3-Embedding-0.6B-GGUF/model.gguf",
                        "model_ready": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MNEMOS_QMD_INDEX", "environment-index")
    monkeypatch.setenv("MNEMOS_QMD_TIMEOUT_SECONDS", "3.5")

    # when
    sut = get_qmd_config(repo_root=str(tmp_path))

    # then
    assert sut == QmdConfig(
        enabled=True,
        executable="/opt/qmd",
        index_name="environment-index",
        mode="vsearch",
        timeout_seconds=3.5,
        update_timeout_seconds=75.0,
        embed_on_update=True,
        embed_model="hf:Qwen/Qwen3-Embedding-0.6B-GGUF/model.gguf",
        model_ready=True,
    )


def test_boundary_case_qmd_config_is_disabled_and_tolerates_malformed_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-106: malformed optional config falls back without enabling QMD."""
    from core.config import QmdConfig, get_qmd_config

    # given
    (tmp_path / "mnemos.yml").write_text(
        yaml.safe_dump(
            {
                "retrieval": {
                    "qmd": {
                        "enabled": "not-a-bool",
                        "index_name": "../escape",
                        "mode": "unknown",
                        "timeout_seconds": -1,
                        "update_timeout_seconds": "invalid",
                        "embed_on_update": 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("MNEMOS_VECTOR_BACKEND", raising=False)

    # when
    sut = get_qmd_config(repo_root=str(tmp_path))

    # then
    assert sut == QmdConfig()


def test_success_case_qmd_config_supports_all_explicit_environment_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every documented override must resolve without requiring YAML mutation."""
    from core.config import QmdConfig, get_qmd_config

    # given
    monkeypatch.setenv("MNEMOS_QMD_ENABLED", "yes")
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "qmd")
    monkeypatch.setenv("MNEMOS_QMD_EXECUTABLE", " /opt/local/qmd ")
    monkeypatch.setenv("MNEMOS_QMD_INDEX", " local-index ")
    monkeypatch.setenv("MNEMOS_QMD_MODE", "VSEARCH")
    monkeypatch.setenv("MNEMOS_QMD_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("MNEMOS_QMD_UPDATE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MNEMOS_QMD_EMBED_ON_UPDATE", "on")
    monkeypatch.setenv("MNEMOS_QMD_EMBED_MODEL", " hf:local/model.gguf ")
    monkeypatch.setenv("MNEMOS_QMD_MODEL_READY", "1")

    # when
    sut = get_qmd_config(repo_root=str(tmp_path))

    # then
    assert sut == QmdConfig(
        enabled=True,
        executable="/opt/local/qmd",
        index_name="local-index",
        mode="vsearch",
        timeout_seconds=2.5,
        update_timeout_seconds=45.0,
        embed_on_update=True,
        embed_model="hf:local/model.gguf",
        model_ready=True,
    )


def test_failure_case_model_backed_search_requires_explicit_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-backed recall must not trigger QMD's first-use downloads implicitly."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    calls_log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QMD_CALLS_LOG", str(calls_log))
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        mode="query",
        model_ready=False,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)
    sut.prepare_index_config({"mnemos-project": tmp_path / "vault"})

    # when
    results = sut.search("모델을 받지 않는 검색", limit=5)

    # then
    assert results == []
    assert sut.diagnostics()["status"] == "model_not_ready"
    assert not calls_log.exists()


def test_success_case_external_qmd_semantic_score_participates_in_ranking() -> None:
    """TC-107: semantic retrieval must not be discarded by lexical re-ranking."""
    from core.retrieval import rank_search_results

    # given
    candidates = [
        {
            "item_id": "lexical",
            "content": "deployment unrelated",
            "metadata": {"layer": "project", "quality_score": 0.8},
        },
        {
            "item_id": "semantic",
            "content": "배포 장애 복구 절차",
            "semantic_score": 0.95,
            "metadata": {"layer": "project", "quality_score": 0.8},
        },
    ]

    # when
    sut = rank_search_results("deployment rollback", candidates, limit=2)

    # then
    assert [item["item_id"] for item in sut] == ["semantic", "lexical"]
    assert sut[0]["metadata"]["score_components"]["semantic"] == 0.95


def test_failure_case_qmd_degradation_preserves_fts_results(
    tmp_path: Path,
) -> None:
    """TC-108: an unavailable QMD process must not break canonical FTS recall."""
    from core.config import QmdConfig
    from core.fts import FTSIndex
    from core.qmd import QmdAdapter
    from core.search import SearchMiddleware
    from core.vector import VectorBackend

    # given
    store = _FileStore({})
    config = QmdConfig(
        enabled=True,
        executable=str(tmp_path / "missing-qmd"),
    )
    QmdAdapter(repo_root=tmp_path, store=store, config=config).prepare_index_config(
        {"mnemos-project": tmp_path / "vault"}
    )
    fts = FTSIndex(db_path=str(tmp_path / "fts.db"))
    fts.index_item(
        item_id="fts-memory",
        content="canonical fallback evidence",
        metadata={"layer": "project"},
    )
    vector = VectorBackend(repo_root=tmp_path, store=store, qmd_config=config)
    sut = SearchMiddleware(
        repo_root=str(tmp_path),
        fts_index=fts,
        vector_backend=vector,
        store=store,
    )

    # when
    results = sut.search("canonical", allow_grep=False)

    # then
    assert [result["item_id"] for result in results] == ["fts-memory"]
    diagnostics = sut.last_diagnostics
    assert diagnostics["status"] == "degraded"
    vector_trace = next(
        backend for backend in diagnostics["backends"] if backend["name"] == "vector"
    )
    assert vector_trace["backend"] == "qmd"
    assert vector_trace["status"] == "missing"


def test_success_case_qmd_search_is_read_only_and_respects_layer_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-109/110: recall reads derived state and applies existing layer policy."""
    from core.config import QmdConfig
    from core.fts import FTSIndex
    from core.qmd import QmdAdapter
    from core.search import SearchMiddleware
    from core.vector import VectorBackend

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    memory_path = tmp_path / "vault" / "global" / "semantic.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("canonical", encoding="utf-8")
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps([{"docid": "#semantic", "score": 0.96, "file": str(memory_path)}]),
    )
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        mode="vsearch",
        model_ready=True,
    )
    store = _FileStore({
        memory_path: {
            "id": "semantic-memory",
            "content": "배포 장애 복구 절차",
            "layer": "global",
            "quality_score": 0.9,
            "_path": str(memory_path),
        }
    })
    QmdAdapter(repo_root=tmp_path, store=store, config=config).prepare_index_config(
        {"mnemos-global": memory_path.parent}
    )
    state_root = tmp_path / ".agent" / "state" / "qmd"
    before = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }
    vector = VectorBackend(repo_root=tmp_path, store=store, qmd_config=config)
    sut = SearchMiddleware(
        repo_root=str(tmp_path),
        fts_index=FTSIndex(db_path=str(tmp_path / "fts.db")),
        vector_backend=vector,
        store=store,
    )

    # when
    allowed = sut.search("deployment rollback", layers=["global"], allow_grep=False)
    denied = sut.search("deployment rollback", layers=["project"], allow_grep=False)
    after = {
        path.relative_to(state_root): path.read_bytes()
        for path in state_root.rglob("*")
        if path.is_file()
    }

    # then
    assert [result["item_id"] for result in allowed] == ["semantic-memory"]
    assert allowed[0]["metadata"]["score_components"]["semantic"] == 0.96
    assert denied == []
    assert after == before


def test_boundary_case_qmd_result_mapping_isolated_to_configured_collections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed, unreadable, and out-of-root hits must be discarded independently."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    collection = tmp_path / "vault" / "project"
    good_path = collection / "good.md"
    missing_path = collection / "missing.md"
    good_path.parent.mkdir(parents=True)
    good_path.write_text("canonical", encoding="utf-8")
    missing_path.write_text("unreadable through store", encoding="utf-8")
    config = QmdConfig(enabled=True, executable=str(executable))
    QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config).prepare_index_config(
        {"mnemos-project": collection}
    )
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps(
            {
                "results": [
                    "not-an-object",
                    {},
                    {"file": "qmd://unknown/ignored.md", "score": 1},
                    {"file": "qmd://mnemos-project/../../outside.md", "score": 1},
                    {"file": "qmd://mnemos-project/missing.md", "score": 1},
                    {"file": "qmd://mnemos-project/good.md", "score": "invalid"},
                ]
            }
        ),
    )
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({good_path: {"content": "canonical", "layer": "project"}}),
        config=config,
    )

    # when
    results = sut.search("mapped query")

    # then
    assert len(results) == 1
    assert results[0]["item_id"] == "good"
    assert results[0]["score"] == 0.0
    assert results[0]["metadata"]["qmd_file"] == "qmd://mnemos-project/good.md"


def test_success_case_qmd_index_update_is_bounded_and_embedding_is_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-111: worker indexing uses argv and embeds only when explicitly enabled."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    calls_log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QMD_CALLS_LOG", str(calls_log))
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        index_name="mnemos-test",
        update_timeout_seconds=_FAKE_QMD_COMPLETION_TIMEOUT_SECONDS,
        embed_on_update=True,
        embed_model="hf:Qwen/Qwen3-Embedding-0.6B-GGUF/model.gguf",
        model_ready=True,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)

    # when
    result = sut.update_index({"mnemos-project": tmp_path / "vault"})

    # then
    calls = [json.loads(line)["argv"] for line in calls_log.read_text().splitlines()]
    assert calls == [
        ["--index", "mnemos-test", "update"],
        ["--index", "mnemos-test", "embed"],
    ]
    assert result == {"updated": True, "embedded": True}
    config_payload = yaml.safe_load(
        (tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos-test.yml").read_text()
    )
    assert config_payload["models"]["embed"] == config.embed_model


def test_failure_case_qmd_index_update_reports_content_free_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TC-112: update failures are retryable without leaking process output."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter, QmdCommandError

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    monkeypatch.setenv("FAKE_QMD_EXIT", "9")
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        update_timeout_seconds=_FAKE_QMD_COMPLETION_TIMEOUT_SECONDS,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)

    # when
    with pytest.raises(QmdCommandError) as captured:
        sut.update_index({"mnemos-project": tmp_path / "vault"})

    # then
    assert captured.value.code == "nonzero_exit"
    assert str(captured.value) == "qmd update failed: nonzero_exit"
    assert "fake qmd failed" not in str(captured.value)


def test_failure_case_qmd_embed_requires_explicit_model_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index refresh may stay lexical, but cannot initiate a model download."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter, QmdCommandError

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    calls_log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_QMD_CALLS_LOG", str(calls_log))
    config = QmdConfig(
        enabled=True,
        executable=str(executable),
        embed_on_update=True,
        model_ready=False,
    )
    sut = QmdAdapter(repo_root=tmp_path, store=_FileStore({}), config=config)

    # when
    with pytest.raises(QmdCommandError) as captured:
        sut.update_index({"mnemos-project": tmp_path / "vault"})

    # then
    calls = [json.loads(line)["argv"] for line in calls_log.read_text().splitlines()]
    assert calls == [["--index", "mnemos", "update"]]
    assert captured.value.code == "model_not_ready"


def test_boundary_case_qmd_search_reports_pending_refresh_as_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A usable but lagging derived index must be explicit in diagnostics."""
    from core.config import QmdConfig
    from core.qmd import QmdAdapter

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    memory_path = tmp_path / "vault" / "project" / "stale.md"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text("canonical", encoding="utf-8")
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps([{"docid": "#stale", "score": 0.8, "file": str(memory_path)}]),
    )
    pending = tmp_path / ".agent" / "state" / "qmd-refresh" / "pending" / "job.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    config = QmdConfig(enabled=True, executable=str(executable))
    sut = QmdAdapter(
        repo_root=tmp_path,
        store=_FileStore({
            memory_path: {
                "id": "stale-memory",
                "content": "canonical content",
                "layer": "project",
                "_path": str(memory_path),
            }
        }),
        config=config,
    )
    sut.prepare_index_config({"mnemos-project": memory_path.parent})

    # when
    results = sut.search("canonical", limit=5)

    # then
    assert [result["item_id"] for result in results] == ["stale-memory"]
    diagnostics = sut.diagnostics()
    assert diagnostics["status"] == "stale"
    assert diagnostics["degraded"] is True
    assert diagnostics["refresh_pending"] == 1
    assert pending.exists()


def test_success_case_qmd_candidates_remain_under_recall_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QMD candidates cannot bypass project, context, trust, or quality policy."""
    from core.config import QmdConfig
    from core.fts import FTSIndex
    from core.qmd import QmdAdapter
    from core.search import SearchMiddleware
    from core.vector import VectorBackend

    # given
    executable = _write_fake_qmd(tmp_path / "fake-qmd")
    paths = {
        item_id: tmp_path / "vault" / "project" / f"{item_id}.md"
        for item_id in ("trusted", "weak", "foreign")
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("canonical", encoding="utf-8")
    items = {
        paths["trusted"]: {
            "id": "trusted",
            "content": "배포 이전 상태 복원 절차",
            "layer": "project",
            "project_id": "project-a",
            "active_files": ["core/deploy.py"],
            "trust_level": "verified",
            "quality_score": 0.95,
            "updated_at": "2099-01-01T00:00:00+00:00",
            "_path": str(paths["trusted"]),
        },
        paths["weak"]: {
            "id": "weak",
            "content": "배포 이전 상태 복원 절차",
            "layer": "project",
            "project_id": "project-a",
            "active_files": ["core/deploy.py"],
            "trust_level": "unverified",
            "quality_score": 0.2,
            "updated_at": "2000-01-01T00:00:00+00:00",
            "_path": str(paths["weak"]),
        },
        paths["foreign"]: {
            "id": "foreign",
            "content": "배포 이전 상태 복원 절차",
            "layer": "project",
            "project_id": "project-b",
            "active_files": ["foreign/file.py"],
            "trust_level": "system",
            "quality_score": 1.0,
            "updated_at": "2099-01-01T00:00:00+00:00",
            "_path": str(paths["foreign"]),
        },
    }
    monkeypatch.setenv(
        "FAKE_QMD_OUTPUT",
        json.dumps(
            [
                {"docid": f"#{item_id}", "score": 0.9, "file": str(path)}
                for item_id, path in paths.items()
            ]
        ),
    )
    config = QmdConfig(enabled=True, executable=str(executable))
    store = _FileStore(items)
    QmdAdapter(repo_root=tmp_path, store=store, config=config).prepare_index_config(
        {"mnemos-project": tmp_path / "vault" / "project"}
    )
    vector = VectorBackend(repo_root=tmp_path, store=store, qmd_config=config)
    search = SearchMiddleware(
        repo_root=str(tmp_path),
        fts_index=FTSIndex(db_path=str(tmp_path / "fts.db")),
        vector_backend=vector,
        store=store,
    )
    from core.gateway import MemoryGateway

    sut = MemoryGateway.__new__(MemoryGateway)
    sut._root = str(tmp_path)
    sut._search = search
    sut._store = store
    sut._session_id = "qmd-policy-test"
    sut._obs = type("Obs", (), {"log_search": lambda *args, **kwargs: None})()

    # when
    report = sut.recall(
        queries=["deployment rollback"],
        layers=["project"],
        project_id="project-a",
        active_files=["core/deploy.py"],
        candidate_limit=5,
        selected_limit=5,
    )

    # then
    assert [candidate.id for candidate in report.candidates] == ["trusted", "weak"]
    assert report.candidates[0].score > report.candidates[1].score
    assert report.candidates[0].score_components["trust"] > report.candidates[1].score_components["trust"]
    assert report.candidates[0].score_components["quality"] > report.candidates[1].score_components["quality"]
    assert report.candidates[0].score_components["recency"] > report.candidates[1].score_components["recency"]
