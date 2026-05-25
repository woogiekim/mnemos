"""Tests for Memory OS architecture contracts and policy helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from core.cli import cli


def test_persistent_memory_contracts_expose_runtime_independent_metadata() -> None:
    """Execution systems can depend on memory contracts, not storage details."""
    from core.contracts import (
        MemoryEnvelope,
        MemoryMetadata,
        PersistentMemoryBackend,
        PersistentMemoryProtocol,
        RetrievalRequest,
        TrustLevel,
        normalize_trust_level,
    )
    from core.store import MemoryStore

    metadata = MemoryMetadata(
        item_id="memory-os-001",
        layer="project",
        trust_level=normalize_trust_level("verified"),
        workflow_id="issue-60",
        provenance={"source": "github-issue"},
    )
    envelope = MemoryEnvelope(
        content="Persistent operational intelligence contract",
        metadata=metadata,
    )
    request = RetrievalRequest(
        query="persistent intelligence",
        layers=("project", "global"),
        workflow_id="issue-60",
    )

    assert envelope.metadata.trust_level is TrustLevel.VERIFIED
    assert request.layers == ("project", "global")
    assert hasattr(PersistentMemoryProtocol, "capture")
    assert hasattr(PersistentMemoryProtocol, "search")
    assert isinstance(MemoryStore(repo_root="."), PersistentMemoryBackend)


def test_lifecycle_manager_preserves_trust_and_plans_transitions() -> None:
    """Lifecycle policy covers promotion guardrails, archive, and expiration."""
    from core.lifecycle import LifecycleAction, MemoryLifecycleManager

    manager = MemoryLifecycleManager()
    now = datetime.now(timezone.utc)

    untrusted = {
        "id": "untrusted",
        "layer": "session",
        "stage": "stored",
        "content": "Short operational memory",
        "trust_level": "unverified",
        "quality_score": 0.95,
        "access_count": 10,
    }
    decision = manager.plan_transition(untrusted, now=now)
    assert decision.action is LifecycleAction.RETAIN
    assert decision.reason == "trust_below_promotion_floor"

    low_quality = {
        "id": "low-quality",
        "layer": "project",
        "stage": "stored",
        "content": "Noisy memory",
        "quality_score": 0.1,
    }
    assert manager.plan_transition(low_quality, now=now).action is LifecycleAction.ARCHIVE

    old_archived = {
        "id": "old-archived",
        "layer": "project",
        "stage": "archived",
        "created_at": (now - timedelta(days=365)).isoformat(),
    }
    expired = manager.plan_transition(old_archived, now=now)
    assert expired.action is LifecycleAction.EXPIRE
    assert expired.target_stage == "expired"


def test_continuity_compressor_preserves_ids_relationships_and_budget() -> None:
    """Compression keeps continuity metadata while reducing context size."""
    from core.compression import ContinuityCompressor

    items = [
        {
            "id": "mem-1",
            "layer": "project",
            "stage": "stored",
            "trust_level": "verified",
            "tags": ["workflow", "retrieval"],
            "workflow_id": "flow-1",
            "quality_score": 0.95,
            "access_count": 5,
            "content": "Workflow-aware retrieval preserves operational continuity and historical awareness.",
        },
        {
            "id": "mem-2",
            "layer": "session",
            "stage": "stored",
            "trust_level": "observed",
            "related_to": ["mem-1"],
            "quality_score": 0.9,
            "content": "Compression should keep relationships between related operational memories.",
        },
    ]

    result = ContinuityCompressor().compress(
        items,
        query="workflow retrieval continuity",
        token_budget=120,
        page_size=2,
    )

    assert result.strategy == "continuity-aware-v1"
    assert result.estimated_tokens <= result.token_budget
    assert result.retained_ids[0] == "mem-1"
    assert "mem-1" in result.pages[0].summary
    assert "workflow" in " ".join(result.pages[0].relationships)


def test_operational_retrieval_ranks_workflow_trust_and_history() -> None:
    """Retrieval ranking optimizes operational continuity over raw lookup."""
    from core.retrieval import OperationalRetrievalRanker

    candidates = [
        {
            "item_id": "generic",
            "content": "memory lifecycle retrieval continuity",
            "metadata": {
                "layer": "project",
                "trust_level": "unverified",
                "quality_score": 0.95,
                "access_count": 0,
                "tags": [],
            },
        },
        {
            "item_id": "operational",
            "content": "memory lifecycle retrieval continuity",
            "metadata": {
                "layer": "project",
                "trust_level": "verified",
                "quality_score": 0.9,
                "access_count": 6,
                "tags": ["workflow", "operational"],
            },
        },
    ]

    ranked = OperationalRetrievalRanker().rank(
        "operational workflow memory lifecycle retrieval continuity",
        candidates,
        workflow_tags=["workflow"],
    )

    assert ranked[0].envelope.metadata.item_id == "operational"
    assert ranked[0].score.workflow > ranked[1].score.workflow
    assert ranked[0].score.trust > ranked[1].score.trust
    assert ranked[0].promotion_hint is True


def test_search_middleware_applies_operational_ranking(tmp_path) -> None:
    """Search results carry operational ranking metadata."""
    from core.fts import FTSIndex
    from core.search import SearchMiddleware

    fts = FTSIndex(db_path=str(tmp_path / "fts.db"))
    fts.index_item(
        item_id="low-trust",
        content="retrieval continuity workflow",
        metadata={
            "layer": "project",
            "trust_level": "unverified",
            "quality_score": 0.8,
            "tags": [],
        },
    )
    fts.index_item(
        item_id="high-trust",
        content="retrieval continuity workflow",
        metadata={
            "layer": "project",
            "trust_level": "verified",
            "quality_score": 0.95,
            "access_count": 5,
            "tags": ["workflow"],
        },
    )

    results = SearchMiddleware(repo_root=str(tmp_path), fts_index=fts).search(
        "retrieval continuity workflow",
        limit=2,
    )

    assert results[0]["item_id"] == "high-trust"
    assert "operational_score" in results[0]
    assert results[0]["metadata"]["score_components"]["trust"] > results[1]["metadata"]["score_components"]["trust"]


def test_provider_capabilities_include_memory_os_surfaces() -> None:
    """Provider metadata exposes the strategic architecture surfaces."""
    result = CliRunner().invoke(cli, ["capabilities", "--json"])

    assert result.exit_code == 0, result.output
    payload = result.output
    assert "persistent_memory_protocol" in payload
    assert "memory_lifecycle_management" in payload
    assert "continuity_compression" in payload
    assert "operational_retrieval" in payload
