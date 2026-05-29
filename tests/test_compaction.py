"""Tests for the semantic compression module (Issue #81 — Stage 1)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from core.compaction import (
    COMPACTION_METHOD_DETERMINISTIC,
    MergePlan,
    MergeResult,
    apply_merge_plan,
    compute_merge_plan,
    derive_merged_layer,
    deterministic_summary,
    restore_source,
    source_path,
)


# ---------------------------------------------------------------------------
# Repo / gateway fixtures (mirrors tests/test_gateway.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    for d in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / d).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for d in ["runs", "sessions", "state", "reports", "tools"]:
        (agent / d).mkdir(parents=True)
    (agent / "workflows" / "hooks").mkdir(parents=True)

    policy = {
        "layers": {
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 0.0, "access_count": 0, "quality_score": 0.0},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy))
    (wiki / "log.md").write_text("# Log\n")
    (wiki / "log.jsonl").write_text("")
    return tmp_path


@pytest.fixture
def gateway(repo_root: Path):
    from core.gateway import MemoryGateway
    return MemoryGateway(repo_root=str(repo_root))


# ---------------------------------------------------------------------------
# deterministic_summary
# ---------------------------------------------------------------------------

class TestDeterministicSummary:
    def test_empty_input_returns_audit_only(self) -> None:
        out = deterministic_summary([])
        assert "## Sources" in out
        assert "(no sources)" in out

    def test_contains_every_distinct_line(self) -> None:
        sources = [
            {"id": "s1", "layer": "session", "created_at": "2026-05-29T00:00Z",
             "content": "alpha\nbeta\ngamma"},
            {"id": "s2", "layer": "session", "created_at": "2026-05-29T01:00Z",
             "content": "delta\nbeta\nepsilon"},
        ]
        out = deterministic_summary(sources)
        for distinct_line in ["alpha", "beta", "gamma", "delta", "epsilon"]:
            assert distinct_line in out

    def test_contains_sources_header_with_every_id(self) -> None:
        sources = [
            {"id": "s1", "layer": "session", "created_at": "t1", "content": "x"},
            {"id": "s2", "layer": "project", "created_at": "t2", "content": "y"},
        ]
        out = deterministic_summary(sources)
        assert "## Sources" in out
        assert "s1" in out and "s2" in out
        # Each entry must record layer + created_at for audit.
        assert "layer=session" in out
        assert "layer=project" in out
        assert "created_at=t1" in out
        assert "created_at=t2" in out

    def test_is_deterministic_across_input_orderings(self) -> None:
        s1 = {"id": "a", "layer": "session", "created_at": "t1", "content": "alpha\nbeta"}
        s2 = {"id": "b", "layer": "session", "created_at": "t2", "content": "beta\ngamma"}
        out_ab = deterministic_summary([s1, s2])
        out_ba = deterministic_summary([s2, s1])
        assert out_ab == out_ba

    def test_dedupes_repeated_lines_globally(self) -> None:
        s1 = {"id": "a", "layer": "session", "created_at": "t", "content": "shared\nunique-a"}
        s2 = {"id": "b", "layer": "session", "created_at": "t", "content": "shared\nunique-b"}
        out = deterministic_summary([s1, s2])
        assert out.count("shared") == 1
        assert "unique-a" in out and "unique-b" in out

    def test_missing_metadata_falls_back_to_placeholder(self) -> None:
        sources = [{"id": "only-id", "content": "x"}]
        out = deterministic_summary(sources)
        # Missing layer/created_at should not crash; emit '?'.
        assert "layer=?" in out
        assert "created_at=?" in out

    def test_trailing_blank_lines_are_trimmed(self) -> None:
        # Content with trailing blank lines exercises the
        # ``merged_lines.pop()`` trim loop in deterministic_summary so
        # the audit header never accumulates extra blank padding.
        sources = [
            {"id": "a", "layer": "session", "created_at": "t",
             "content": "alpha\n\n\n"},
        ]
        out = deterministic_summary(sources)
        # Between alpha and the Sources header there must be exactly one
        # blank line (not three) regardless of trailing whitespace.
        assert "alpha\n\n## Sources" in out


# ---------------------------------------------------------------------------
# derive_merged_layer
# ---------------------------------------------------------------------------

class TestDeriveMergedLayer:
    def test_empty_input_raises(self, gateway: Any) -> None:
        with pytest.raises(ValueError):
            derive_merged_layer([], gateway._policy)

    def test_single_layer_short_circuit(self, gateway: Any) -> None:
        sources = [{"id": "a", "layer": "session"}, {"id": "b", "layer": "session"}]
        assert derive_merged_layer(sources, gateway._policy) == "session"

    def test_picks_highest_layer_via_policy(self, gateway: Any) -> None:
        # session promotes to project promotes to global.
        sources = [
            {"id": "a", "layer": "session"},
            {"id": "b", "layer": "project"},
        ]
        # project is downstream of session — it is the "highest".
        assert derive_merged_layer(sources, gateway._policy) == "project"

    def test_working_and_session_promotes_to_session(self, gateway: Any) -> None:
        sources = [
            {"id": "a", "layer": "working"},
            {"id": "b", "layer": "session"},
        ]
        assert derive_merged_layer(sources, gateway._policy) == "session"

    def test_session_and_project_promotes_to_project(self, gateway: Any) -> None:
        sources = [
            {"id": "a", "layer": "session"},
            {"id": "b", "layer": "project"},
        ]
        assert derive_merged_layer(sources, gateway._policy) == "project"

    def test_three_layers_picks_terminal(self, gateway: Any) -> None:
        sources = [
            {"id": "a", "layer": "working"},
            {"id": "b", "layer": "session"},
            {"id": "c", "layer": "project"},
        ]
        assert derive_merged_layer(sources, gateway._policy) == "project"

    def test_unknown_layer_treated_terminally(self, gateway: Any) -> None:
        # An unknown source layer simply contributes its own name as a
        # candidate — derive_merged_layer must not crash.
        sources = [
            {"id": "a", "layer": "session"},
            {"id": "b", "layer": "unknown-layer-xyz"},
        ]
        out = derive_merged_layer(sources, gateway._policy)
        # Result is deterministic but layer identity depends on inbound
        # counts; assert no crash + valid string.
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# compute_merge_plan — pure dry run
# ---------------------------------------------------------------------------

class TestComputeMergePlan:
    def test_empty_group_raises(self, gateway: Any) -> None:
        with pytest.raises(ValueError):
            compute_merge_plan(gateway, [])

    def test_unknown_summarizer_raises(self, gateway: Any) -> None:
        with pytest.raises(ValueError):
            compute_merge_plan(
                gateway,
                [{"id": "a", "layer": "session", "content": "x"}],
                summarizer="bogus",
            )

    def test_pure_no_writes(self, gateway: Any, repo_root: Path) -> None:
        # Capture two items, then compute a plan — compute_merge_plan
        # itself must not add any new files anywhere in the store.
        id1 = gateway.capture(layer="session", content="alpha beta gamma")
        id2 = gateway.capture(layer="session", content="alpha beta delta")
        group = [gateway.read(id1), gateway.read(id2)]

        def snapshot_all_md() -> set[Path]:
            return set(repo_root.rglob("*.md"))

        before = snapshot_all_md()
        plan = compute_merge_plan(gateway, group)
        after = snapshot_all_md()

        assert before == after, (
            f"compute_merge_plan must not write files; new={after - before} "
            f"removed={before - after}"
        )
        assert plan.merged_id is None
        assert plan.method == COMPACTION_METHOD_DETERMINISTIC
        assert set(plan.sources) == {id1, id2}

    def test_explicit_layer_pin_overrides_derivation(self, gateway: Any) -> None:
        id1 = gateway.capture(layer="session", content="alpha")
        group = [gateway.read(id1)]
        plan = compute_merge_plan(gateway, group, layer="global")
        assert plan.target_layer == "global"

    def test_llm_summarizer_falls_back_to_deterministic_when_unavailable(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the LLM helper to return None (network unavailable / no key)
        # — the deterministic path MUST run silently.
        from core import compaction as compaction_mod

        monkeypatch.setattr(compaction_mod, "_llm_summary", lambda sources: None)
        id1 = gateway.capture(layer="session", content="alpha")
        group = [gateway.read(id1)]
        plan = compute_merge_plan(gateway, group, summarizer="llm")
        assert plan.method == COMPACTION_METHOD_DETERMINISTIC
        assert "## Sources" in plan.content

    def test_llm_summarizer_uses_llm_path_when_available(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import compaction as compaction_mod

        monkeypatch.setattr(
            compaction_mod, "_llm_summary",
            lambda sources: "LLM-merged body text",
        )
        id1 = gateway.capture(layer="session", content="alpha")
        group = [gateway.read(id1)]
        plan = compute_merge_plan(gateway, group, summarizer="llm")
        assert plan.method == compaction_mod.COMPACTION_METHOD_LLM
        assert "LLM-merged body text" in plan.content
        # Audit trail still appears even on the LLM path.
        assert "## Sources" in plan.content


# ---------------------------------------------------------------------------
# apply_merge_plan
# ---------------------------------------------------------------------------

class TestApplyMergePlan:
    def test_apply_writes_merged_and_supersedes_sources(
        self, gateway: Any, repo_root: Path
    ) -> None:
        id1 = gateway.capture(layer="session", content="alpha beta gamma")
        id2 = gateway.capture(layer="session", content="alpha beta delta")

        group = [gateway.read(id1), gateway.read(id2)]
        plan = compute_merge_plan(gateway, group)
        result = apply_merge_plan(gateway, plan)

        assert result.applied is True
        assert isinstance(result, MergeResult)
        assert result.merged_id
        assert set(result.sources) == {id1, id2}

        # The merged memory must carry sources + compaction_method.
        merged = gateway.read(result.merged_id)
        assert merged.get("sources") == list(result.sources) or set(merged.get("sources", [])) == set(result.sources)
        assert merged.get("compaction_method") == COMPACTION_METHOD_DETERMINISTIC

        # Each source must now be archived AND back-reference the merged id.
        for src_id in (id1, id2):
            src = gateway.read(src_id)
            assert src.get("stage") == "archived"
            assert src.get("superseded_by") == result.merged_id

    def test_apply_rejects_plan_with_existing_merged_id(self, gateway: Any) -> None:
        plan = MergePlan(
            sources=("a",),
            target_layer="session",
            content="x",
            method=COMPACTION_METHOD_DETERMINISTIC,
            merged_id="already-set",
        )
        with pytest.raises(ValueError):
            apply_merge_plan(gateway, plan)

    def test_apply_raises_when_capture_returns_none(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a dedup short-circuit: capture returns None.
        monkeypatch.setattr(
            gateway, "capture", lambda **kwargs: None,
        )
        plan = MergePlan(
            sources=("x",),
            target_layer="session",
            content="anything",
            method=COMPACTION_METHOD_DETERMINISTIC,
        )
        with pytest.raises(RuntimeError):
            apply_merge_plan(gateway, plan)

    def test_apply_tolerates_source_that_disappears_after_archive(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Capture two real sources, then monkey-patch ``gateway.archive``
        # so that after archiving, the source file is deleted from disk —
        # the post-archive ``store.read(source_id)`` will then raise
        # FileNotFoundError and the defensive ``continue`` branch fires.
        id1 = gateway.capture(layer="session", content="alpha one source")
        id2 = gateway.capture(layer="session", content="alpha two source")
        assert id1 and id2

        group = [gateway.read(id1), gateway.read(id2)]
        plan = compute_merge_plan(gateway, group)

        original_archive = gateway.archive

        def archive_then_unlink(source_id: str) -> None:
            # Run the real archive (flips stage, writes superseded log)
            # then physically remove the underlying file so the supervisor
            # call to store.read raises FileNotFoundError on its next hop.
            item = gateway._store.read(source_id)
            path = item.get("_path")
            original_archive(source_id)
            if path:
                from pathlib import Path
                Path(path).unlink(missing_ok=True)

        monkeypatch.setattr(gateway, "archive", archive_then_unlink)
        # apply_merge_plan must complete even though the source file is
        # missing — the merged memory's `sources` list remains the
        # authoritative lineage record.
        result = apply_merge_plan(gateway, plan)
        assert result.applied is True
        assert result.merged_id

    def test_idempotent_re_run_skips_already_archived_sources(
        self, gateway: Any
    ) -> None:
        from core.similarity import group_similar

        # Two near-identical (but not exactly identical) captures so the
        # gateway's NFKC dedup does not collapse them into one id.
        id1 = gateway.capture(layer="session", content="alpha beta gamma one")
        id2 = gateway.capture(layer="session", content="alpha beta gamma two")
        assert id1 is not None and id2 is not None

        group = [gateway.read(id1), gateway.read(id2)]
        plan = compute_merge_plan(gateway, group)
        apply_merge_plan(gateway, plan)

        # Re-run group detection over the same store — the archived+
        # superseded sources must be skipped by find_similar_pairs.
        items = [gateway.read(id1), gateway.read(id2)]
        groups = group_similar(items, threshold=0.5)
        assert groups == []


# ---------------------------------------------------------------------------
# restore_source
# ---------------------------------------------------------------------------

class TestRestoreSource:
    def test_returns_archived_snapshot_with_superseded_pointer(
        self, gateway: Any
    ) -> None:
        id1 = gateway.capture(layer="session", content="alpha one")
        id2 = gateway.capture(layer="session", content="alpha two")
        assert id1 is not None and id2 is not None
        plan = compute_merge_plan(gateway, [gateway.read(id1), gateway.read(id2)])
        result = apply_merge_plan(gateway, plan)

        snap = restore_source(gateway, id1)
        assert snap["id"] == id1
        assert snap["stage"] == "archived"
        assert snap["superseded_by"] == result.merged_id
        # Original content body must survive the archive.
        assert "alpha" in snap["content"]

    def test_restore_inserts_none_when_no_back_pointer(self, gateway: Any) -> None:
        id1 = gateway.capture(layer="session", content="alpha")
        snap = restore_source(gateway, id1)
        # No supersede has happened — superseded_by should be normalised to None.
        assert snap["superseded_by"] is None


# ---------------------------------------------------------------------------
# source_path
# ---------------------------------------------------------------------------

class TestSourcePath:
    def test_returns_existing_path(self, gateway: Any) -> None:
        id1 = gateway.capture(layer="session", content="alpha")
        path = source_path(gateway, id1)
        assert path is not None and path.exists()

    def test_returns_none_for_unknown_id(self, gateway: Any) -> None:
        assert source_path(gateway, "no-such-id-anywhere") is None

    def test_returns_none_when_path_field_was_unlinked(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The store reports a path that no longer exists on disk — the
        # helper must return None instead of a stale path the caller
        # would unlink/read against in error.
        monkeypatch.setattr(
            gateway._store, "read",
            lambda _id: {"_path": "/var/tmp/does-not-exist-anywhere-xyz.md"},
        )
        assert source_path(gateway, "any-id") is None

    def test_returns_none_when_store_returns_no_path(
        self, gateway: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty/missing _path field must also degrade to None.
        monkeypatch.setattr(
            gateway._store, "read",
            lambda _id: {"_path": ""},
        )
        assert source_path(gateway, "any-id") is None
