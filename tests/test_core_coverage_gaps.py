"""Focused coverage for core edge paths that protect operational reliability."""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest


def test_vector_backend_diagnostics_for_success_failure_and_search_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.vector import VectorBackend, _format_error

    class FakeQdrantClient:
        def __init__(self, url: str) -> None:
            self.url = url

    qdrant_module = types.SimpleNamespace(QdrantClient=FakeQdrantClient)
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_module)
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("MNEMOS_QDRANT_URL", "http://vector")
    qdrant = VectorBackend()

    assert qdrant.is_available is True
    assert qdrant.backend_name == "qdrant"
    assert qdrant.search("continuity") == []
    assert qdrant.diagnostics()["status"] == "available"

    def broken_qdrant(query: str, limit: int) -> list[dict[str, Any]]:
        raise RuntimeError("search failed")

    monkeypatch.setattr(qdrant, "_search_qdrant", broken_qdrant)
    assert qdrant.search("continuity") == []
    assert qdrant.diagnostics()["status"] == "degraded"

    class BrokenQdrantClient:
        def __init__(self, url: str) -> None:
            raise RuntimeError("offline")

    monkeypatch.setitem(sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=BrokenQdrantClient))
    unavailable = VectorBackend()
    assert unavailable.is_available is False
    assert unavailable.diagnostics()["status"] == "unavailable"

    class FakeChromaClient:
        def __init__(self, path: str) -> None:
            self.path = path

    monkeypatch.setitem(sys.modules, "chromadb", types.SimpleNamespace(PersistentClient=FakeChromaClient))
    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "chroma")
    monkeypatch.setenv("MNEMOS_CHROMA_PATH", "/tmp/chroma")
    chroma = VectorBackend()
    assert chroma.is_available is True
    assert chroma.search("continuity") == []

    monkeypatch.setitem(
        sys.modules,
        "chromadb",
        types.SimpleNamespace(PersistentClient=lambda path: (_ for _ in ()).throw(RuntimeError("bad chroma"))),
    )
    unavailable_chroma = VectorBackend()
    assert unavailable_chroma.diagnostics()["status"] == "unavailable"

    chroma._backend = "custom"
    chroma._available = True
    chroma._client = object()
    assert chroma.search("continuity") == []

    monkeypatch.setenv("MNEMOS_VECTOR_BACKEND", "weaviate")
    unsupported = VectorBackend()
    assert unsupported.diagnostics()["status"] == "unsupported"
    assert unsupported.search("anything") == []
    assert _format_error(RuntimeError()) == "RuntimeError"


def test_compressor_truncates_first_item_and_drops_later_over_budget() -> None:
    from core.compression import ContinuityCompressor, estimate_tokens

    items = [
        {"id": "large", "layer": "project", "content": "A" * 200, "trust_level": "verified"},
        {"id": "later", "layer": "project", "content": "B" * 200, "trust_level": "verified"},
    ]

    result = ContinuityCompressor().compress(items, token_budget=2, max_item_chars=200)

    assert result.retained_ids == ("large",)
    assert result.dropped_ids == ("later",)
    assert result.pages[0].summary.endswith("...")
    assert estimate_tokens("") == 0

    partially_full = ContinuityCompressor().compress(
        [
            {"id": "small", "content": "ok"},
            {"id": "too-large", "content": "B" * 200},
        ],
        token_budget=20,
        max_item_chars=200,
    )
    assert partially_full.retained_ids == ("small",)
    assert partially_full.dropped_ids == ("too-large",)


def test_config_loader_handles_disabled_yaml_and_unreadable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.config as config

    monkeypatch.setattr(config, "_HAS_YAML", False)
    assert config._load_yaml_config(str(tmp_path)) == {}

    monkeypatch.setattr(config, "_HAS_YAML", True)
    cfg_path = tmp_path / "mnemos.yml"
    cfg_path.write_text("storage:\n  backend: obsidian\n", encoding="utf-8")

    def fail_open(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot read")

    monkeypatch.setattr(Path, "open", fail_open)
    assert config._load_yaml_config(str(tmp_path)) == {}


def test_contract_output_provider_and_retrieval_edge_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib import metadata

    from core.contracts import TrustLevel, normalize_trust_level
    from core.output import promote_notice
    from core.provider import (
        _capability_status,
        package_version,
        provider_error_from_exception,
        search_payload,
    )
    from core.retrieval import OperationalRetrievalRanker, rank_search_results

    assert normalize_trust_level(TrustLevel.SYSTEM) is TrustLevel.SYSTEM
    assert normalize_trust_level("unknown-trust") is TrustLevel.OBSERVED
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert promote_notice("item", "project").startswith("\033[94m")
    assert _capability_status(False) == "unsupported"
    assert _capability_status("experimental") == "unknown"

    def missing_package(name: str) -> str:
        raise metadata.PackageNotFoundError

    monkeypatch.setattr(metadata, "version", missing_package)
    assert package_version() == "unknown"
    assert provider_error_from_exception(RuntimeError("backend broke"))["error"]["code"] == "backend_error"

    empty_payload = search_payload(query="q", results=[], mode="standard")
    one_payload = search_payload(query="q", results=[{"item_id": "one"}], mode="standard")
    diag_payload = search_payload(
        query="q",
        results=[{"item_id": "one"}, {"item_id": "two"}],
        mode="fast",
        retrieval_diagnostics={"partial_failure": True},
    )
    assert empty_payload["results"] == []
    assert one_payload["results"][0]["score"] == 1.0
    assert diag_payload["status"] == "degraded"

    ranker = OperationalRetrievalRanker()
    ranked = ranker.rank(
        "",
        [{"item_id": "bad", "content": "", "metadata": {"created_at": "not-a-date", "access_count": "bad"}}],
        limit=1,
    )
    assert ranked[0].reason == "low_signal"
    assert ranked[0].score.recency == 0.5

    decayed = ranker.rank(
        "old",
        [{"item_id": "old", "content": "old", "metadata": {"created_at": "2000-01-01T00:00:00Z"}}],
    )
    assert decayed[0].score.recency == 0.05

    assert rank_search_results("q", [], limit=2) == []


def test_policy_engine_rejects_edge_transitions(tmp_path: Path) -> None:
    import yaml

    from core.policy import PolicyEngine, PolicyViolationError

    with pytest.raises(FileNotFoundError):
        PolicyEngine(str(tmp_path / "missing.yml"))

    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(
        yaml.dump(
            {
                "layers": {
                    "project": {
                        "promotes_to": "global",
                        "demotes_to": "session",
                        "path_template": "wiki/projects",
                        "promotion": {"age_hours": 999, "access_count": 3, "quality_score": 0.9},
                    },
                    "global": {"promotes_to": None, "path_template": "wiki/global", "promotion": {}},
                    "session": {"promotes_to": "project", "path_template": ".agent/sessions", "promotion": {}},
                },
                "forget": {"requires_archived": True},
            }
        ),
        encoding="utf-8",
    )
    policy = PolicyEngine(str(policy_path))

    with pytest.raises(PolicyViolationError):
        policy.validate_capture("missing", {})
    with pytest.raises(PolicyViolationError):
        policy.validate_promote({"layer": "missing"}, "global")
    with pytest.raises(PolicyViolationError):
        policy.validate_promote({"layer": "project", "created_at": "2999-01-01T00:00:00Z"}, "global")
    with pytest.raises(PolicyViolationError):
        policy.validate_promote({"layer": "project", "access_count": 0}, "global")
    with pytest.raises(PolicyViolationError):
        policy.validate_promote({"layer": "project", "access_count": 3, "quality_score": 0.1}, "global")
    with pytest.raises(PolicyViolationError):
        policy.validate_demote({"layer": "project"}, "global")
    with pytest.raises(PolicyViolationError):
        policy.validate_demote({"layer": "project"}, "project")
    with pytest.raises(PolicyViolationError):
        policy.validate_forget({"stage": "stored"})
    with pytest.raises(PolicyViolationError):
        policy.check_lifecycle_stage({"stage": "stored"}, "archived")
    with pytest.raises(PolicyViolationError):
        policy.get_next_layer("missing")
    with pytest.raises(PolicyViolationError):
        policy.get_layer_path_template("missing")

    assert policy.check_promotion_eligible({"layer": "missing"}) is False
    assert policy.check_promotion_eligible({"layer": "global"}) is False
    assert policy.check_promotion_eligible({"layer": "project", "created_at": "not-a-date"}) is False
    assert policy.get_layer_path_template("project") == "wiki/projects"


def test_hook_dispatcher_filters_scripts_and_handles_start_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.hooks import HookDispatcher

    event_dir = tmp_path / ".agent" / "workflows" / "hooks" / "post-capture"
    event_dir.mkdir(parents=True)
    (event_dir / "subdir").mkdir()
    executable = event_dir / "run"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    ignored = event_dir / "note.txt"
    ignored.write_text("skip", encoding="utf-8")

    started: list[list[str]] = []

    class FakeProc:
        def wait(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: Any) -> FakeProc:
        started.append(args)
        return FakeProc()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    HookDispatcher(str(tmp_path)).fire("post-capture", {"item_id": "abc"})
    assert started == [[str(executable)]]

    def fail_popen(*args: Any, **kwargs: Any) -> FakeProc:
        raise OSError("cannot start")

    monkeypatch.setattr("subprocess.Popen", fail_popen)
    HookDispatcher(str(tmp_path)).fire("post-capture", {"item_id": "abc"})

    def fail_unexpected(*args: Any, **kwargs: Any) -> FakeProc:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("subprocess.Popen", fail_unexpected)
    HookDispatcher(str(tmp_path)).fire("post-capture", {"item_id": "abc"})
