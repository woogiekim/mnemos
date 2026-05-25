"""Deterministic host-injectable context retrieval for mnemos."""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.gateway import MemoryGateway
from core.provider import PROVIDER_CONTRACT_VERSION, SCORE_SCALE

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "must", "can", "to", "of", "in", "on", "at",
    "by", "for", "from", "with", "and", "or", "but", "not", "so", "if",
    "as", "it", "its", "this", "that", "these", "those", "i", "you", "we",
    "they", "my", "your", "our", "their", "what", "how", "why", "when",
    "where", "which", "please", "implement", "fix", "add", "update",
}


def extract_keywords(prompt: str, *, limit: int = 5) -> list[str]:
    """Return deterministic search keywords from a prompt."""
    try:
        from core.korean import preprocess_query
    except Exception:
        preprocess_query = None  # type: ignore[assignment]

    prompt = re.sub(r"([a-z])([A-Z])", r"\1 \2", prompt[:500])
    prompt = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", prompt)
    seen: set[str] = set()
    keywords: list[str] = []
    for token in re.split(r"[\s\W]+", prompt):
        if len(token) < 2 or token.lower() in _STOPWORDS:
            continue
        expanded = preprocess_query(token) if preprocess_query is not None else token
        for word in expanded.split():
            lowered = word.lower()
            if len(word) < 2 or lowered in _STOPWORDS or lowered in seen:
                continue
            seen.add(lowered)
            keywords.append(word)
    keywords.sort(key=lambda value: (-len(value), value.lower()))
    return keywords[:limit]


def _score(index: int, count: int) -> float:
    if count <= 1:
        return 1.0 if count == 1 else 0.0
    return round(1.0 - (index / (count - 1)), 6)


def _enrich(gw: MemoryGateway, result: dict[str, Any], score: float) -> dict[str, Any]:
    item_id = result.get("item_id") or result.get("id") or ""
    metadata = result.get("metadata") or {}
    item: dict[str, Any] = {}
    if item_id:
        try:
            item = gw._store.read(str(item_id))
        except Exception:
            item = {}
    return {
        "id": item.get("id") or item_id,
        "layer": item.get("layer") or metadata.get("layer") or result.get("layer"),
        "score": score,
        "recency": item.get("created_at") or item.get("updated_at") or metadata.get("created_at"),
        "content": item.get("content") or result.get("content", ""),
    }


def retrieve_context(
    *,
    prompt: str,
    session_id: str | None,
    host: str,
    gateway: MemoryGateway | None = None,
    limit: int = 5,
    max_chars: int = 1800,
) -> dict[str, Any]:
    """Return bounded deterministic context for host adapters."""
    repo_root = os.environ.get("MNEMOS_REPO_ROOT") or "."
    try:
        gw = gateway or MemoryGateway()
        repo_root = str(gw._root)
    except Exception:
        gw = None
        partial_failure = True
    else:
        partial_failure = False
    keywords = extract_keywords(prompt)
    query = " ".join(keywords) if keywords else prompt[:120]
    if gw is None:
        raw_results = []
    else:
        try:
            raw_results = gw.search(query=query, limit=max(limit, 1)) if query else []
            if not raw_results:
                seen_ids: set[str] = set()
                fallback_results: list[dict[str, Any]] = []
                for keyword in keywords:
                    for result in gw.search(query=keyword, limit=max(limit, 1)):
                        result_id = str(result.get("item_id") or result.get("id") or "")
                        if result_id in seen_ids:
                            continue
                        seen_ids.add(result_id)
                        fallback_results.append(result)
                        if len(fallback_results) >= limit:
                            break
                    if len(fallback_results) >= limit:
                        break
                raw_results = fallback_results
        except Exception:
            raw_results = []
            partial_failure = True

    used_chars = 0
    results: list[dict[str, Any]] = []
    for index, result in enumerate(raw_results[:limit]):
        if gw is None:
            break
        item = _enrich(gw, result, _score(index, len(raw_results)))
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if not content:
            continue
        remaining = max_chars - used_chars
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[: max(0, remaining - 3)].rstrip() + "..."
        used_chars += len(content)
        item["content"] = content
        results.append(item)

    return {
        "provider": "mnemos",
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status": "degraded" if partial_failure else "ok",
        "mode": "deterministic-v1",
        "host": host,
        "session_id": session_id,
        "repo_root": repo_root,
        "prompt": prompt,
        "query": query,
        "keywords": keywords,
        "count": len(results),
        "max_chars": max_chars,
        "score_scale": SCORE_SCALE,
        "results": results,
    }


def render_context_block(payload: dict[str, Any]) -> str:
    """Render a bounded ``<mnemos-context>`` block."""
    attrs = {
        "mode": payload.get("mode", "deterministic-v1"),
        "host": payload.get("host") or "",
        "session-id": payload.get("session_id") or "",
        "query": payload.get("query") or "",
        "count": str(payload.get("count", 0)),
    }
    attr_text = " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in attrs.items() if value != "")
    lines = [f"<mnemos-context {attr_text}>".rstrip()]
    for item in payload.get("results", []):
        memory_attrs = {
            "id": str(item.get("id") or ""),
            "layer": str(item.get("layer") or ""),
            "score": str(item.get("score") or 0.0),
            "recency": str(item.get("recency") or ""),
        }
        memory_attr_text = " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in memory_attrs.items() if value != "")
        lines.append(f"  <memory {memory_attr_text}>{html.escape(str(item.get('content') or ''))}</memory>".rstrip())
    lines.append("</mnemos-context>")

    promotion_block = render_promotion_block(repo_root=payload.get("repo_root"))
    if promotion_block:
        lines.extend(["", promotion_block])

    return "\n".join(lines)


def _promotion_cursor_path() -> Path:
    """Return the promotion cursor path used by host prompt hooks."""
    override = os.environ.get("MNEMOS_PROMO_CURSOR")
    if override:
        return Path(override)
    return Path.home() / ".mnemos" / ".cache" / "promotion-cursor.txt"


def render_promotion_block(*, repo_root: str | None) -> str:
    """Render newly promoted memories since the last prompt hook cursor.

    This preserves the old UserPromptSubmit promotion visibility feature while
    keeping the shell hook thin: the hook delegates all context rendering to the
    host-neutral ``mnemos context`` command.
    """
    if not repo_root:
        return ""

    obs_log = Path(repo_root) / "wiki" / "observability.jsonl"
    if not obs_log.exists():
        return ""

    cursor_path = _promotion_cursor_path()
    cursor_ts = "2020-01-01T00:00:00Z"
    if cursor_path.exists():
        cursor_ts = cursor_path.read_text(encoding="utf-8").strip() or cursor_ts

    promotions: list[str] = []
    try:
        for raw in obs_log.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("event") != "promotion":
                continue
            ts = str(entry.get("ts") or "")
            if ts <= cursor_ts:
                continue
            memory_id = entry.get("memory_id") or entry.get("item_id")
            layer = entry.get("layer") or entry.get("to_layer")
            if memory_id and layer:
                promotions.append(f"{memory_id} → {layer}")
    except OSError:
        return ""

    if not promotions:
        return ""

    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass

    lines = ["<mnemos-promotion>"]
    lines.extend(f"<promotion>{html.escape(item)}</promotion>" for item in promotions)
    lines.append("</mnemos-promotion>")
    return "\n".join(lines)
