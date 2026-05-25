"""Continuity-aware context compression for operational memory."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.contracts import TRUST_RANK, normalize_trust_level


@dataclass(frozen=True)
class MemoryPage:
    """A compressed page that preserves memory identity and relationships."""

    page_id: str
    item_ids: tuple[str, ...]
    summary: str
    estimated_tokens: int
    relationships: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompressionResult:
    """Result of continuity-aware memory compression."""

    pages: tuple[MemoryPage, ...]
    retained_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...]
    token_budget: int
    estimated_tokens: int
    strategy: str = "continuity-aware-v1"
    metadata: dict[str, Any] = field(default_factory=dict)


class ContinuityCompressor:
    """Compress memory collections while preserving operational continuity."""

    def compress(
        self,
        items: list[dict[str, Any]],
        *,
        query: str = "",
        token_budget: int = 512,
        page_size: int = 4,
        max_item_chars: int = 180,
    ) -> CompressionResult:
        """Return compressed pages ordered by operational relevance."""
        token_budget = max(0, token_budget)
        page_size = max(1, page_size)
        ordered = sorted(
            items,
            key=lambda item: self._priority(item, query),
            reverse=True,
        )

        retained: list[dict[str, Any]] = []
        dropped_ids: list[str] = []
        used_tokens = 0

        for item in ordered:
            line = self._condense_item(item, max_chars=max_item_chars)
            estimated = estimate_tokens(line)
            remaining = token_budget - used_tokens
            if remaining <= 0:
                dropped_ids.append(item_id(item))
                continue
            if estimated > remaining:
                if not retained:
                    max_chars = max(0, remaining * 4 - 3)
                    truncated = line[:max_chars].rstrip() + "..." if max_chars else ""
                    item = dict(item)
                    item["_compressed_line"] = truncated
                    retained.append(item)
                    used_tokens += estimate_tokens(truncated)
                else:
                    dropped_ids.append(item_id(item))
                continue

            item = dict(item)
            item["_compressed_line"] = line
            retained.append(item)
            used_tokens += estimated

        pages: list[MemoryPage] = []
        for page_index in range(0, len(retained), page_size):
            chunk = retained[page_index: page_index + page_size]
            lines = [str(item.get("_compressed_line") or self._condense_item(item)) for item in chunk]
            relationships = tuple(
                sorted({
                    rel
                    for item in chunk
                    for rel in self._relationships(item)
                })
            )
            summary = "\n".join(lines)
            pages.append(
                MemoryPage(
                    page_id=f"page-{len(pages) + 1}",
                    item_ids=tuple(item_id(item) for item in chunk),
                    summary=summary,
                    estimated_tokens=estimate_tokens(summary),
                    relationships=relationships,
                )
            )

        return CompressionResult(
            pages=tuple(pages),
            retained_ids=tuple(item_id(item) for item in retained),
            dropped_ids=tuple(dropped_ids),
            token_budget=token_budget,
            estimated_tokens=sum(page.estimated_tokens for page in pages),
            metadata={
                "input_count": len(items),
                "retained_count": len(retained),
                "dropped_count": len(dropped_ids),
            },
        )

    def _condense_item(self, item: dict[str, Any], *, max_chars: int = 180) -> str:
        memory_id = item_id(item)
        layer = str(item.get("layer") or item.get("metadata", {}).get("layer") or "unknown")
        stage = str(item.get("stage") or item.get("metadata", {}).get("stage") or "stored")
        trust = normalize_trust_level(item.get("trust_level") or item.get("metadata", {}).get("trust_level")).value
        content = " ".join(str(item.get("content") or "").split())
        if len(content) > max_chars:
            content = content[: max(0, max_chars - 3)].rstrip() + "..."
        return f"[{memory_id}] {layer}/{stage} trust={trust}: {content}"

    def _priority(self, item: dict[str, Any], query: str) -> float:
        query_terms = set(_tokens(query))
        content_terms = set(_tokens(str(item.get("content") or "")))
        tag_terms = set(str(tag).lower() for tag in item.get("tags") or item.get("metadata", {}).get("tags") or [])
        semantic = len(query_terms & (content_terms | tag_terms)) / max(1, len(query_terms)) if query_terms else 0.5
        quality = _clamp_float(item.get("quality_score") or item.get("metadata", {}).get("quality_score"), 0.8)
        access = min(1.0, _safe_int(item.get("access_count") or item.get("metadata", {}).get("access_count"), 0) / 5)
        trust = TRUST_RANK[normalize_trust_level(item.get("trust_level") or item.get("metadata", {}).get("trust_level"))] / max(TRUST_RANK.values())
        return semantic * 0.4 + quality * 0.25 + access * 0.2 + trust * 0.15

    def _relationships(self, item: dict[str, Any]) -> list[str]:
        relationships: list[str] = []
        for key in ("workflow_id", "session_id", "run_id", "related_to"):
            value = item.get(key) or item.get("metadata", {}).get(key)
            if isinstance(value, list):
                relationships.extend(str(v) for v in value if v)
            elif value:
                relationships.append(f"{key}:{value}")
        for tag in item.get("tags") or item.get("metadata", {}).get("tags") or []:
            relationships.append(f"tag:{tag}")
        return relationships


def item_id(item: dict[str, Any]) -> str:
    """Return the durable item id from a memory-shaped dict."""
    return str(item.get("id") or item.get("item_id") or item.get("metadata", {}).get("id") or "unknown")


def estimate_tokens(text: str) -> int:
    """Return a deterministic rough token estimate."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_./-]{2,}", text)]


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
