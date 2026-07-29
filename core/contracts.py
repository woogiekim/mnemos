"""Persistent memory contracts for the Mnemos Memory OS architecture."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class TrustLevel(str, Enum):
    """Trust boundaries for operational memory records."""

    UNVERIFIED = "unverified"
    OBSERVED = "observed"
    INFERRED = "inferred"
    VERIFIED = "verified"
    SYSTEM = "system"


TRUST_RANK: dict[TrustLevel, int] = {
    TrustLevel.UNVERIFIED: 0,
    TrustLevel.INFERRED: 1,
    TrustLevel.OBSERVED: 2,
    TrustLevel.VERIFIED: 3,
    TrustLevel.SYSTEM: 4,
}


def normalize_trust_level(value: str | TrustLevel | None) -> TrustLevel:
    """Return a known trust level, defaulting to observed memory."""
    if isinstance(value, TrustLevel):
        return value
    try:
        return TrustLevel(str(value or TrustLevel.OBSERVED.value).lower())
    except ValueError:
        return TrustLevel.OBSERVED


class LifecycleStage(str, Enum):
    """Lifecycle stages used by persistent memory contracts."""

    CAPTURED = "captured"
    CLASSIFIED = "classified"
    STORED = "stored"
    SUMMARIZED = "summarized"
    COMPRESSED = "compressed"
    RETRIEVED = "retrieved"
    USED = "used"
    VALIDATED = "validated"
    PROMOTED = "promoted"
    DEMOTED = "demoted"
    ARCHIVED = "archived"
    EXPIRED = "expired"
    FORGOTTEN = "forgotten"


class MemoryLayer(str, Enum):
    """Runtime-independent memory tiers."""

    TRANSIENT = "transient"
    EPHEMERAL = "ephemeral"
    WORKING = "working"
    SESSION = "session"
    PROJECT = "project"
    GLOBAL = "global"
    ENTITIES = "entities"
    CLAIMS = "claims"
    TOPICS = "topics"


@dataclass(frozen=True)
class MemoryMetadata:
    """Durable metadata that can travel across runtimes and backends."""

    item_id: str
    layer: str
    stage: str = LifecycleStage.STORED.value
    trust_level: TrustLevel = TrustLevel.OBSERVED
    tags: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    workflow_id: str | None = None
    source: str | None = None
    confidence: float = 0.8
    quality_score: float = 0.8


@dataclass(frozen=True)
class MemoryEnvelope:
    """A memory record plus contract metadata."""

    content: str
    metadata: MemoryMetadata


@dataclass(frozen=True)
class RetrievalRequest:
    """Runtime-independent retrieval contract."""

    query: str
    layers: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    workflow_id: str | None = None
    continuity_scope: str | None = None
    limit: int = 20
    min_score: float = 0.0


@dataclass(frozen=True)
class RetrievalScore:
    """Score components for workflow-aware retrieval."""

    total: float
    semantic: float
    recency: float
    trust: float
    workflow: float
    historical: float
    quality: float
    decay: float


@dataclass(frozen=True)
class RetrievalMatch:
    """A ranked retrieval result that preserves continuity metadata."""

    envelope: MemoryEnvelope
    score: RetrievalScore
    reason: str
    promotion_hint: bool = False
    decay_hint: bool = False


@dataclass(frozen=True)
class RecallMemory:
    """One read-only recall candidate or selected memory."""

    id: str
    layer: str
    stage: str
    content: str
    score: float
    matched_queries: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    project_id: str | None = None
    project_root_hash: str | None = None
    semantic_status: str | None = None
    task_shape: str | None = None
    agent_role: str | None = None
    active_files: tuple[str, ...] = ()
    created_at: str | None = None
    updated_at: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class RecallReport:
    """Read-only recall result separating candidates from selected memories."""

    queries: tuple[str, ...]
    candidates: tuple[RecallMemory, ...]
    selected: tuple[RecallMemory, ...]
    candidate_limit: int
    selected_limit: int
    max_selected_chars: int
    used_chars: int
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LifecycleTransition:
    """A lifecycle transition request or result."""

    item_id: str
    action: str
    from_layer: str | None = None
    to_layer: str | None = None
    from_stage: str | None = None
    to_stage: str | None = None
    reason: str = ""
    metadata_updates: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PersistentMemoryBackend(Protocol):
    """Storage backend contract independent from filesystem layout."""

    def write(
        self,
        layer: str,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Persist memory content and return the backend-specific locator."""
        ...

    def read(self, item_id_or_path: str) -> dict[str, Any]:
        """Return content and metadata for one memory item."""
        ...

    def update(
        self,
        item_id_or_path: str,
        content: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> Any:
        """Update content and/or metadata for one memory item."""
        ...

    def delete(self, item_id_or_path: str) -> None:
        """Remove one memory item from the backend."""
        ...

    def iter_layer_items(
        self,
        layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Yield parsed memory items for a layer."""
        ...


@runtime_checkable
class PersistentMemoryProtocol(Protocol):
    """High-level persistent memory contract for execution systems."""

    def capture(
        self,
        content: str,
        layer: str | None = None,
        item_id: str | None = None,
        tags: list[str] | None = None,
        quality_score: float = 0.8,
        run_id: str | None = None,
        session_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        no_classify: bool = False,
    ) -> str | None:
        """Capture memory through the lifecycle contract."""
        ...

    def search(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve memories through the stable retrieval contract."""
        ...

    def promote(
        self,
        item_id: str,
        target_layer: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        force: bool = False,
    ) -> str:
        """Promote one memory through lifecycle policy."""
        ...

    def archive(self, item_id: str) -> None:
        """Archive one memory without destroying historical continuity."""
        ...
