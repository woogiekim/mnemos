"""Managed lifecycle policy for persistent operational memory."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from core.contracts import LifecycleStage, TrustLevel, TRUST_RANK, normalize_trust_level


class LifecycleAction(str, Enum):
    """Actions produced by the managed memory lifecycle planner."""

    RETAIN = "retain"
    SUMMARIZE = "summarize"
    COMPRESS = "compress"
    PROMOTE = "promote"
    ARCHIVE = "archive"
    EXPIRE = "expire"


@dataclass(frozen=True)
class LifecycleRule:
    """Retention, promotion, and compression policy for a layer."""

    layer: str
    next_layer: str | None = None
    summarize_after_chars: int = 900
    compress_after_chars: int = 1800
    promote_min_access: int = 3
    promote_min_quality: float = 0.85
    archive_below_quality: float = 0.25
    archive_after_days_without_access: int = 90
    expire_archived_after_days: int = 180


@dataclass(frozen=True)
class LifecyclePolicy:
    """Managed memory lifecycle policy independent from storage backends."""

    rules: dict[str, LifecycleRule]
    promotion_trust_floor: TrustLevel = TrustLevel.OBSERVED
    tier_order: tuple[str, ...] = (
        "transient",
        "ephemeral",
        "working",
        "session",
        "project",
        "global",
    )
    metadata_keys: tuple[str, ...] = (
        "lifecycle_action",
        "lifecycle_reason",
        "trust_level",
        "last_lifecycle_at",
    )

    @classmethod
    def default(cls) -> "LifecyclePolicy":
        """Return the default Memory OS lifecycle policy."""
        transitions = {
            "transient": None,
            "ephemeral": "working",
            "working": "session",
            "session": "project",
            "project": "global",
            "global": None,
            "entities": None,
            "claims": None,
            "topics": None,
        }
        return cls(
            rules={
                layer: LifecycleRule(layer=layer, next_layer=next_layer)
                for layer, next_layer in transitions.items()
            }
        )

    def rule_for(self, layer: str) -> LifecycleRule:
        """Return the rule for a layer, falling back to a retaining rule."""
        return self.rules.get(layer, LifecycleRule(layer=layer))


@dataclass(frozen=True)
class LifecycleDecision:
    """A lifecycle decision plus metadata needed to apply it."""

    action: LifecycleAction
    reason: str
    target_layer: str | None = None
    target_stage: str | None = None
    metadata_updates: dict[str, Any] = field(default_factory=dict)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp into an aware UTC datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_days(value: Any, *, now: datetime | None = None) -> float | None:
    """Return age in days, or None when the timestamp is unknown."""
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - parsed).total_seconds() / 86400.0)


class MemoryLifecycleManager:
    """Plan lifecycle transitions without depending on a storage backend."""

    def __init__(self, policy: LifecyclePolicy | None = None) -> None:
        self._policy = policy or LifecyclePolicy.default()

    def plan_transition(
        self,
        item: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> LifecycleDecision:
        """Return the next managed lifecycle action for a memory item."""
        reference = now or datetime.now(timezone.utc)
        layer = str(item.get("layer") or "ephemeral")
        stage = str(item.get("stage") or LifecycleStage.STORED.value)
        rule = self._policy.rule_for(layer)
        trust = normalize_trust_level(item.get("trust_level"))
        quality = _clamp_float(item.get("quality_score"), 0.8)
        access_count = _safe_int(item.get("access_count"), 0)
        content = str(item.get("content") or "")
        created_age = age_days(item.get("created_at"), now=reference)
        last_access_age = age_days(item.get("last_access_at"), now=reference)
        inactive_days = last_access_age if last_access_age is not None else created_age

        if stage == LifecycleStage.ARCHIVED.value:
            if created_age is not None and created_age >= rule.expire_archived_after_days:
                return self._decision(
                    LifecycleAction.EXPIRE,
                    "archived_memory_exceeded_retention",
                    target_stage=LifecycleStage.EXPIRED.value,
                    trust=trust,
                )
            return self._decision(
                LifecycleAction.RETAIN,
                "archived_memory_within_retention",
                trust=trust,
            )

        if quality < rule.archive_below_quality:
            return self._decision(
                LifecycleAction.ARCHIVE,
                "quality_below_archive_threshold",
                target_stage=LifecycleStage.ARCHIVED.value,
                trust=trust,
            )

        if (
            inactive_days is not None
            and inactive_days >= rule.archive_after_days_without_access
            and access_count == 0
        ):
            return self._decision(
                LifecycleAction.ARCHIVE,
                "memory_inactive_beyond_retention_window",
                target_stage=LifecycleStage.ARCHIVED.value,
                trust=trust,
            )

        if len(content) >= rule.compress_after_chars:
            return self._decision(
                LifecycleAction.COMPRESS,
                "content_exceeds_compression_threshold",
                target_stage=LifecycleStage.COMPRESSED.value,
                trust=trust,
            )

        if len(content) >= rule.summarize_after_chars:
            return self._decision(
                LifecycleAction.SUMMARIZE,
                "content_exceeds_summary_threshold",
                target_stage=LifecycleStage.SUMMARIZED.value,
                trust=trust,
            )

        if access_count >= rule.promote_min_access and quality >= rule.promote_min_quality:
            if self._trust_below(trust, self._policy.promotion_trust_floor):
                return self._decision(
                    LifecycleAction.RETAIN,
                    "trust_below_promotion_floor",
                    trust=trust,
                )
            if rule.next_layer is not None:
                return self._decision(
                    LifecycleAction.PROMOTE,
                    "promotion_thresholds_met",
                    target_layer=rule.next_layer,
                    target_stage=LifecycleStage.PROMOTED.value,
                    trust=trust,
                )

        return self._decision(
            LifecycleAction.RETAIN,
            "no_lifecycle_transition_required",
            trust=trust,
        )

    def summarize_item(self, item: dict[str, Any], *, max_chars: int = 320) -> str:
        """Return a deterministic operational summary for one memory item."""
        item_id = str(item.get("id") or item.get("item_id") or "unknown")
        layer = str(item.get("layer") or "unknown")
        stage = str(item.get("stage") or LifecycleStage.STORED.value)
        trust = normalize_trust_level(item.get("trust_level")).value
        content = " ".join(str(item.get("content") or "").split())
        if len(content) > max_chars:
            content = content[: max(0, max_chars - 3)].rstrip() + "..."
        return f"{item_id} [{layer}/{stage}, trust={trust}]: {content}"

    def _decision(
        self,
        action: LifecycleAction,
        reason: str,
        *,
        trust: TrustLevel,
        target_layer: str | None = None,
        target_stage: str | None = None,
    ) -> LifecycleDecision:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        updates = {
            "lifecycle_action": action.value,
            "lifecycle_reason": reason,
            "trust_level": trust.value,
            "last_lifecycle_at": now,
        }
        if target_stage is not None:
            updates["stage"] = target_stage
        if target_layer is not None:
            updates["target_layer"] = target_layer
        return LifecycleDecision(
            action=action,
            reason=reason,
            target_layer=target_layer,
            target_stage=target_stage,
            metadata_updates=updates,
        )

    @staticmethod
    def _trust_below(actual: TrustLevel, required: TrustLevel) -> bool:
        return TRUST_RANK[actual] < TRUST_RANK[required]


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
