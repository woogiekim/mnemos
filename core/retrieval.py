"""Workflow-aware operational retrieval ranking."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.contracts import (
    MemoryEnvelope,
    MemoryMetadata,
    RetrievalMatch,
    RetrievalScore,
    TRUST_RANK,
    normalize_trust_level,
)


@dataclass(frozen=True)
class OperationalRetrievalPolicy:
    """Weights for operational memory retrieval."""

    semantic_weight: float = 0.35
    recency_weight: float = 0.15
    trust_weight: float = 0.15
    workflow_weight: float = 0.15
    historical_weight: float = 0.1
    quality_weight: float = 0.1
    decay_after_days: int = 120
    promotion_access_threshold: int = 3


class OperationalRetrievalRanker:
    """Rank memories for continuity, not just raw lookup."""

    def __init__(self, policy: OperationalRetrievalPolicy | None = None) -> None:
        self._policy = policy or OperationalRetrievalPolicy()

    def rank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        *,
        workflow_tags: list[str] | None = None,
        limit: int | None = None,
    ) -> list[RetrievalMatch]:
        """Return ranked retrieval matches with score components."""
        matches = [
            self.match(query, candidate, workflow_tags=workflow_tags)
            for candidate in candidates
        ]
        matches.sort(
            key=lambda match: (
                match.score.total,
                match.score.semantic,
                match.score.trust,
                match.envelope.metadata.updated_at or match.envelope.metadata.created_at or "",
            ),
            reverse=True,
        )
        if limit is not None:
            return matches[:limit]
        return matches

    def match(
        self,
        query: str,
        candidate: dict[str, Any],
        *,
        workflow_tags: list[str] | None = None,
    ) -> RetrievalMatch:
        """Score one candidate and return a retrieval match."""
        metadata = candidate.get("metadata") or {}
        tags = tuple(str(tag) for tag in (candidate.get("tags") or metadata.get("tags") or ()))
        trust = normalize_trust_level(candidate.get("trust_level") or metadata.get("trust_level"))
        quality = _clamp_float(candidate.get("quality_score") or metadata.get("quality_score"), 0.8)
        access_count = _safe_int(candidate.get("access_count") or metadata.get("access_count"), 0)
        created_at = _first_text(candidate.get("created_at"), metadata.get("created_at"), candidate.get("recency"))
        updated_at = _first_text(candidate.get("updated_at"), metadata.get("updated_at"))
        content = str(candidate.get("content") or "")

        lexical_semantic = _semantic_score(query, content, tags)
        external_semantic = _clamp_float(candidate.get("semantic_score"), 0.0)
        semantic = max(lexical_semantic, external_semantic)
        recency = _recency_score(updated_at or created_at, self._policy.decay_after_days)
        trust_score = TRUST_RANK[trust] / max(TRUST_RANK.values())
        workflow = _workflow_score(query, tags, workflow_tags, metadata)
        historical = min(1.0, access_count / 5)
        decay = 1.0 - recency

        total = (
            semantic * self._policy.semantic_weight
            + recency * self._policy.recency_weight
            + trust_score * self._policy.trust_weight
            + workflow * self._policy.workflow_weight
            + historical * self._policy.historical_weight
            + quality * self._policy.quality_weight
        )
        score = RetrievalScore(
            total=round(max(0.0, min(1.0, total)), 6),
            semantic=round(semantic, 6),
            recency=round(recency, 6),
            trust=round(trust_score, 6),
            workflow=round(workflow, 6),
            historical=round(historical, 6),
            quality=round(quality, 6),
            decay=round(decay, 6),
        )
        envelope = MemoryEnvelope(
            content=content,
            metadata=MemoryMetadata(
                item_id=str(candidate.get("item_id") or candidate.get("id") or metadata.get("id") or ""),
                layer=str(candidate.get("layer") or metadata.get("layer") or ""),
                stage=str(candidate.get("stage") or metadata.get("stage") or "stored"),
                trust_level=trust,
                tags=tags,
                provenance=dict(candidate.get("provenance") or metadata.get("provenance") or {}),
                created_at=created_at,
                updated_at=updated_at,
                workflow_id=_first_text(candidate.get("workflow_id"), metadata.get("workflow_id")),
                source=_first_text(candidate.get("source"), metadata.get("source")),
                confidence=_clamp_float(candidate.get("confidence") or metadata.get("confidence"), quality),
                quality_score=quality,
            ),
        )
        return RetrievalMatch(
            envelope=envelope,
            score=score,
            reason=_reason(score),
            promotion_hint=access_count >= self._policy.promotion_access_threshold and quality >= 0.85,
            decay_hint=decay >= 0.75,
        )


def rank_search_results(
    query: str,
    results: list[dict[str, Any]],
    *,
    workflow_tags: list[str] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank SearchMiddleware result dicts and preserve provider shape."""
    ranker = OperationalRetrievalRanker()
    scored: list[tuple[RetrievalMatch, dict[str, Any]]] = [
        (ranker.match(query, result, workflow_tags=workflow_tags), result)
        for result in results
    ]
    scored.sort(
        key=lambda pair: (
            pair[0].score.total,
            pair[0].score.semantic,
            pair[0].score.trust,
            pair[0].envelope.metadata.updated_at or pair[0].envelope.metadata.created_at or "",
        ),
        reverse=True,
    )

    ranked: list[dict[str, Any]] = []
    for match, result in scored[:limit]:
        item = dict(result)
        metadata = dict(item.get("metadata") or {})
        metadata["operational_score"] = match.score.total
        metadata["score_components"] = {
            "semantic": match.score.semantic,
            "recency": match.score.recency,
            "trust": match.score.trust,
            "workflow": match.score.workflow,
            "historical": match.score.historical,
            "quality": match.score.quality,
            "decay": match.score.decay,
        }
        metadata["promotion_hint"] = match.promotion_hint
        metadata["decay_hint"] = match.decay_hint
        item["metadata"] = metadata
        item["operational_score"] = match.score.total
        ranked.append(item)
    return ranked


def _semantic_score(query: str, content: str, tags: tuple[str, ...]) -> float:
    terms = set(_tokens(query))
    if not terms:
        return 0.0
    haystack = set(_tokens(content)) | {tag.lower() for tag in tags}
    return len(terms & haystack) / max(1, len(terms))


def _workflow_score(
    query: str,
    tags: tuple[str, ...],
    workflow_tags: list[str] | None,
    metadata: dict[str, Any],
) -> float:
    expected = {tag.lower() for tag in workflow_tags or []}
    expected.update(token for token in _tokens(query) if token in {"workflow", "operational", "continuity"})
    if not expected:
        return 0.0
    observed = {tag.lower() for tag in tags}
    for key in ("workflow_id", "session_id", "run_id"):
        value = metadata.get(key)
        if value:
            observed.add(str(value).lower())
    return min(1.0, len(expected & observed) / max(1, len(expected)))


def _recency_score(value: str | None, decay_after_days: int) -> float:
    if not value:
        return 0.5
    parsed = _parse_dt(value)
    if parsed is None:
        return 0.5
    age_days = max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)
    if age_days >= decay_after_days:
        return 0.05
    return max(0.05, 1.0 - (age_days / decay_after_days))


def _parse_dt(value: str) -> datetime | None:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _reason(score: RetrievalScore) -> str:
    parts: list[str] = []
    if score.semantic >= 0.5:
        parts.append("semantic_match")
    if score.workflow > 0:
        parts.append("workflow_relevant")
    if score.historical > 0:
        parts.append("historically_used")
    if score.trust >= 0.75:
        parts.append("trusted")
    return ",".join(parts) or "low_signal"


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_./-]{2,}", text)]


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is not None:
            return str(value)
    return None


def _clamp_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
