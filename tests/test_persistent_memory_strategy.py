"""Phase 1 tests for Mnemos as persistent operational memory infrastructure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from core.compression import ContinuityCompressor
from core.context import retrieve_context
from core.contracts import TrustLevel
from core.gateway import MemoryGateway
from core.lifecycle import LifecycleAction, MemoryLifecycleManager
from core.retrieval import OperationalRetrievalRanker
from core.search import SearchMiddleware
from core.store import MemoryStore


@pytest.fixture
def memory_os_repo(tmp_path: Path) -> Path:
    """Create a minimal Memory OS repository for strategy tests."""
    wiki = tmp_path / "wiki"
    for dirname in ["global", "projects", "entities", "claims", "topics"]:
        (wiki / dirname).mkdir(parents=True)

    agent = tmp_path / ".agent"
    for dirname in ["runs", "sessions", "state", "reports", "tools", "transient"]:
        (agent / dirname).mkdir(parents=True)

    policy = {
        "layers": {
            "transient": {
                "path_template": ".agent/transient/",
                "promotes_to": None,
                "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 1.0},
            },
            "ephemeral": {
                "path_template": ".agent/runs/{run_id}/scratch/",
                "promotes_to": "working",
                "promotion": {"age_hours": 1.0, "access_count": 1, "quality_score": 0.8},
            },
            "working": {
                "path_template": ".agent/runs/{run_id}/working/",
                "promotes_to": "session",
                "promotion": {"age_hours": 1.0, "access_count": 2, "quality_score": 0.8},
            },
            "session": {
                "path_template": ".agent/sessions/{session_id}/",
                "promotes_to": "project",
                "promotion": {"age_hours": 1.0, "access_count": 3, "quality_score": 0.85},
            },
            "project": {
                "path_template": "wiki/projects/",
                "promotes_to": "global",
                "promotion": {"age_hours": 1.0, "access_count": 5, "quality_score": 0.9},
            },
            "global": {
                "path_template": "wiki/global/",
                "promotes_to": None,
                "promotion": {"age_hours": 1.0, "access_count": 5, "quality_score": 0.9},
            },
        },
        "forget": {"requires_archived": True},
        "archive": {"allowed_stages": ["stored", "retrieved", "used", "validated", "compressed", "summarized"]},
    }
    (wiki / "policy.yaml").write_text(yaml.dump(policy), encoding="utf-8")
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    (wiki / "log.jsonl").write_text("", encoding="utf-8")

    return tmp_path


def test_cross_session_context_preserves_operational_continuity(
    memory_os_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later session can retrieve a prior operational decision."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(memory_os_repo))
    first_session = MemoryGateway(repo_root=str(memory_os_repo))
    first_session.capture(
        layer="project",
        item_id="decision-continuity-001",
        content=(
            "Architecture decision: Mnemos preserves workflow continuity "
            "through persistent memory contracts across sessions."
        ),
        tags=["architecture", "workflow", "continuity"],
        quality_score=0.96,
        session_id="session-a",
        extra_metadata={
            "trust_level": TrustLevel.VERIFIED.value,
            "workflow_id": "memory-os-phase-1",
            "confidence": 0.96,
        },
        no_classify=True,
    )
    first_session.capture(
        layer="project",
        item_id="decoy-vector-count",
        content="Vector storage count and embedding volume are secondary metrics.",
        tags=["storage"],
        quality_score=0.7,
        session_id="session-a",
        no_classify=True,
    )

    later_session = MemoryGateway(repo_root=str(memory_os_repo))
    payload = retrieve_context(
        prompt="Continue the workflow continuity architecture decision",
        session_id="session-b",
        host="test",
        gateway=later_session,
        limit=3,
    )

    result_ids = [item["id"] for item in payload["results"]]
    assert "decision-continuity-001" in result_ids
    assert payload["selection"]["selected_count"] >= 1
    assert payload["results"][result_ids.index("decision-continuity-001")]["score"] > 0


def test_lifecycle_policy_covers_phase_one_transition_questions() -> None:
    """Lifecycle management covers summarize, compress, promote, archive, and expire."""
    manager = MemoryLifecycleManager()
    now = datetime.now(timezone.utc)

    summarize = manager.plan_transition({
        "id": "summary-candidate",
        "layer": "session",
        "stage": "stored",
        "trust_level": "verified",
        "quality_score": 0.9,
        "access_count": 1,
        "content": "summarize " * 120,
    }, now=now)
    assert summarize.action is LifecycleAction.SUMMARIZE

    compress = manager.plan_transition({
        "id": "compression-candidate",
        "layer": "project",
        "stage": "stored",
        "trust_level": "verified",
        "quality_score": 0.9,
        "access_count": 1,
        "content": "compress " * 260,
    }, now=now)
    assert compress.action is LifecycleAction.COMPRESS

    promote = manager.plan_transition({
        "id": "promotion-candidate",
        "layer": "session",
        "stage": "stored",
        "trust_level": "verified",
        "quality_score": 0.95,
        "access_count": 6,
        "content": "Short trusted operational memory.",
    }, now=now)
    assert promote.action is LifecycleAction.PROMOTE
    assert promote.target_layer == "project"

    archive = manager.plan_transition({
        "id": "archive-candidate",
        "layer": "project",
        "stage": "stored",
        "quality_score": 0.1,
        "content": "Low quality operational noise.",
    }, now=now)
    assert archive.action is LifecycleAction.ARCHIVE

    expire = manager.plan_transition({
        "id": "expire-candidate",
        "layer": "project",
        "stage": "archived",
        "created_at": (now - timedelta(days=220)).isoformat(),
    }, now=now)
    assert expire.action is LifecycleAction.EXPIRE


def test_workflow_aware_retrieval_prioritizes_operational_relevance() -> None:
    """Retrieval ranks continuity, workflow fit, trust, and history above generic hits."""
    candidates = [
        {
            "item_id": "generic-semantic-hit",
            "content": "memory lifecycle retrieval continuity workflow",
            "metadata": {
                "layer": "project",
                "trust_level": "unverified",
                "quality_score": 0.95,
                "access_count": 0,
                "tags": [],
            },
        },
        {
            "item_id": "operational-history-hit",
            "content": "memory lifecycle retrieval continuity workflow",
            "metadata": {
                "layer": "project",
                "trust_level": "verified",
                "quality_score": 0.9,
                "access_count": 7,
                "tags": ["workflow", "operational", "continuity"],
                "workflow_id": "memory-os-phase-1",
            },
        },
    ]

    ranked = OperationalRetrievalRanker().rank(
        "continue operational workflow memory lifecycle retrieval continuity",
        candidates,
        workflow_tags=["workflow", "operational"],
    )

    assert ranked[0].envelope.metadata.item_id == "operational-history-hit"
    assert ranked[0].score.workflow > ranked[1].score.workflow
    assert ranked[0].score.historical > ranked[1].score.historical
    assert ranked[0].score.trust > ranked[1].score.trust


def test_context_compression_preserves_meaning_and_relationships() -> None:
    """Compression retains key decisions, item identity, and relationship metadata."""
    items = [
        {
            "id": "historical-decision",
            "layer": "project",
            "stage": "stored",
            "trust_level": "verified",
            "tags": ["architecture", "workflow", "continuity"],
            "workflow_id": "memory-os-phase-1",
            "quality_score": 0.97,
            "access_count": 8,
            "content": (
                "Historical architecture decision: persistent memory contracts "
                "preserve operational continuity across model generations."
            ),
        },
        {
            "id": "follow-up-summary",
            "layer": "session",
            "stage": "summarized",
            "trust_level": "observed",
            "related_to": ["historical-decision"],
            "tags": ["summary"],
            "quality_score": 0.9,
            "content": (
                "Workflow summary: later retrieval must reconstruct why "
                "the architecture chose persistent contracts."
            ),
        },
        {
            "id": "storage-decoy",
            "layer": "project",
            "stage": "stored",
            "tags": ["storage"],
            "quality_score": 0.4,
            "content": "Raw storage growth and embedding counts are secondary concerns.",
        },
    ]

    result = ContinuityCompressor().compress(
        items,
        query="persistent memory contracts operational continuity",
        token_budget=140,
        page_size=2,
    )

    assert "historical-decision" in result.retained_ids
    assert result.estimated_tokens <= result.token_budget
    page_text = "\n".join(page.summary for page in result.pages)
    relationship_text = " ".join(rel for page in result.pages for rel in page.relationships)
    assert "persistent memory contracts" in page_text
    assert "historical-decision" in page_text
    assert "workflow_id:memory-os-phase-1" in relationship_text


def test_historical_awareness_reconstructs_prior_architecture_context(
    memory_os_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context retrieval can reconstruct prior decisions and workflow outcomes."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(memory_os_repo))
    gateway = MemoryGateway(repo_root=str(memory_os_repo))
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat().replace("+00:00", "Z")

    gateway.capture(
        layer="project",
        item_id="prior-provider-contract",
        content=(
            "Prior architecture decision: provider contracts stay stable so "
            "execution systems depend on persistent memory contracts."
        ),
        tags=["architecture", "contract", "history"],
        quality_score=0.95,
        extra_metadata={"trust_level": "verified", "created_at": old, "confidence": 0.95},
        no_classify=True,
    )
    gateway.capture(
        layer="project",
        item_id="prior-workflow-outcome",
        content=(
            "Historical workflow outcome: issue resolution required lifecycle "
            "management and workflow-aware retrieval, not raw vector lookup."
        ),
        tags=["workflow", "history", "retrieval"],
        quality_score=0.9,
        extra_metadata={"trust_level": "observed", "created_at": old, "confidence": 0.9},
        no_classify=True,
    )

    payload = retrieve_context(
        prompt="Explain prior architecture provider contracts and workflow-aware retrieval",
        session_id="history-restore",
        host="test",
        gateway=gateway,
        limit=4,
    )

    ids = {item["id"] for item in payload["results"]}
    assert "prior-provider-contract" in ids
    assert "prior-workflow-outcome" in ids


def test_long_running_memory_evolution_keeps_initial_decision_retrievable(
    memory_os_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow can evolve over time without losing its initial decision."""
    monkeypatch.setenv("MNEMOS_REPO_ROOT", str(memory_os_repo))
    gateway = MemoryGateway(repo_root=str(memory_os_repo))
    now = datetime.now(timezone.utc)

    records = [
        (
            "day-0-decision",
            now - timedelta(days=45),
            "Initial decision: persistent memory contracts preserve continuity.",
        ),
        (
            "day-14-summary",
            now - timedelta(days=31),
            "Summary: lifecycle management keeps operational memory relevant.",
        ),
        (
            "day-45-implementation",
            now,
            "Implementation note: workflow-aware retrieval restores context.",
        ),
    ]
    for item_id, created_at, content in records:
        gateway.capture(
            layer="project",
            item_id=item_id,
            content=content,
            tags=["long-running", "continuity", "workflow"],
            quality_score=0.93,
            extra_metadata={
                "trust_level": "verified",
                "workflow_id": "long-running-memory-os",
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "confidence": 0.93,
            },
            no_classify=True,
        )

    payload = retrieve_context(
        prompt="Restore persistent memory contracts workflow-aware retrieval context",
        session_id="day-45",
        host="test",
        gateway=gateway,
        limit=5,
    )

    ids = [item["id"] for item in payload["results"]]
    assert "day-0-decision" in ids
    assert "day-45-implementation" in ids


def test_partial_storage_corruption_does_not_break_retrieval_fallback(
    memory_os_repo: Path,
) -> None:
    """Retrieval survives a corrupt memory file when a valid memory remains."""
    store = MemoryStore(repo_root=str(memory_os_repo))
    store.write(
        layer="project",
        item_id="valid-resilience-memory",
        content="Resilient valid memory survives partial storage inconsistency.",
        metadata={
            "id": "valid-resilience-memory",
            "layer": "project",
            "stage": "stored",
            "trust_level": "verified",
            "tags": ["resilience"],
        },
    )
    corrupt = memory_os_repo / "wiki" / "projects" / "corrupt-memory.md"
    corrupt.write_text(
        "---\nid: corrupt-memory\nlayer: project\ntags: [broken\n---\n"
        "This malformed file should not stop retrieval.\n",
        encoding="utf-8",
    )

    results = SearchMiddleware(
        repo_root=str(memory_os_repo),
        store=store,
    ).search(
        "resilient valid memory",
        layers=["project"],
        limit=5,
    )

    assert [result["item_id"] for result in results] == ["valid-resilience-memory"]
    assert results[0]["operational_score"] > 0
