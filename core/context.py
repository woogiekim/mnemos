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

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 8
_MAX_CONTEXT_CHARS = 2400
_MAX_MEMORY_CHARS = 700
_MIN_SELECTION_SCORE = 0.22
_STALE_DAYS = 365
_FRESH_DAYS = 30

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "must", "can", "to", "of", "in", "on", "at",
    "by", "for", "from", "with", "and", "or", "but", "not", "so", "if",
    "as", "it", "its", "this", "that", "these", "those", "i", "you", "we",
    "they", "my", "your", "our", "their", "what", "how", "why", "when",
    "where", "which", "please", "implement", "fix", "add", "update",
}

_NOISE_PATTERNS = [
    re.compile(r"^\s*(?:status|plan|blocker|review|task_id|task_dir|branch|requirements)\s*:", re.I | re.M),
    re.compile(r"\[agent-crew\]\s+(?:stop|route)\b", re.I),
    re.compile(r"</?mnemos-[^>]+>|</?mnemos-context[^>]*>", re.I),
    re.compile(r"\b(?:traceback \(most recent call last\)|stack trace)\b", re.I),
]


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


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _clamp_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1]
            if "+" not in text and "-" not in text[10:]:
                text += "+00:00"
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_days(value: Any) -> float | None:
    parsed = _parse_dt(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)


def _freshness_score(value: Any) -> float:
    age = _age_days(value)
    if age is None:
        return 0.45
    if age <= _FRESH_DAYS:
        return 1.0
    if age >= _STALE_DAYS:
        return 0.15
    return round(1.0 - ((age - _FRESH_DAYS) / (_STALE_DAYS - _FRESH_DAYS)) * 0.85, 6)


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_./-]{2,}", text)]


def _signal_density(content: str, keywords: list[str]) -> float:
    tokens = _tokenize(content)
    if not tokens:
        return 0.0

    unique_keywords = {keyword.lower() for keyword in keywords if len(keyword) >= 2}
    hits = sum(1 for token in tokens if token in unique_keywords)
    hit_score = min(1.0, hits / max(1, min(len(unique_keywords), 5)))
    length_score = 1.0 if len(tokens) <= 90 else max(0.25, 90 / len(tokens))
    return round((hit_score * 0.7) + (length_score * 0.3), 6)


def _relevance_score(item: dict[str, Any], keywords: list[str], fallback: float) -> float:
    content = str(item.get("content") or "")
    haystack = " ".join([
        content,
        str(item.get("id") or ""),
        " ".join(str(tag) for tag in item.get("tags") or []),
    ]).lower()
    if not keywords:
        return fallback

    keyword_set = {keyword.lower() for keyword in keywords}
    matches = sum(1 for keyword in keyword_set if keyword in haystack)
    lexical = matches / max(1, len(keyword_set))
    return round(max(fallback * 0.65, lexical), 6)


def _noise_score(content: str) -> float:
    if not content.strip():
        return 1.0

    pattern_hits = sum(1 for pattern in _NOISE_PATTERNS if pattern.search(content))
    lines = [line for line in content.splitlines() if line.strip()]
    control_lines = sum(1 for line in lines if re.match(r"^\s*(?:STATUS|PLAN|TASK|BRANCH|REQUIREMENTS)\s*:", line, re.I))
    control_ratio = control_lines / max(1, len(lines))
    return min(1.0, (pattern_hits * 0.35) + control_ratio)


def _selection_score(
    item: dict[str, Any],
    *,
    keywords: list[str],
    rank_score: float,
) -> tuple[float, dict[str, float]]:
    quality = _clamp_float(item.get("quality_score"), 0.8)
    confidence = _clamp_float(item.get("confidence"), quality)
    freshness = _freshness_score(item.get("updated_at") or item.get("created_at") or item.get("recency"))
    relevance = _relevance_score(item, keywords, rank_score)
    signal = _signal_density(str(item.get("content") or ""), keywords)
    noise = _noise_score(str(item.get("content") or ""))

    score = (
        relevance * 0.34
        + freshness * 0.18
        + confidence * 0.18
        + quality * 0.12
        + signal * 0.18
    ) * (1.0 - (noise * 0.75))
    components = {
        "relevance": round(relevance, 6),
        "freshness": round(freshness, 6),
        "confidence": round(confidence, 6),
        "signal_density": round(signal, 6),
        "noise": round(noise, 6),
    }
    return round(max(0.0, min(1.0, score)), 6), components


def _skip_reason(score: float, item: dict[str, Any], content: str, components: dict[str, float]) -> str | None:
    if not content:
        return "empty"
    if components["noise"] >= 0.5:
        return "noisy"
    age = _age_days(item.get("updated_at") or item.get("created_at") or item.get("recency"))
    if (
        age is not None
        and age >= _STALE_DAYS
        and components["freshness"] < 0.2
        and (score < 0.45 or components["confidence"] < 0.5)
    ):
        return "stale"
    if score < _MIN_SELECTION_SCORE:
        return "low_signal"
    return None


def _search_diagnostics(gw: Any, query: str) -> dict[str, Any] | None:
    diagnostics = getattr(gw, "last_search_diagnostics", None)
    if not isinstance(diagnostics, dict):
        return None

    payload = dict(diagnostics)
    payload["query"] = query
    return payload


def _combine_retrieval_diagnostics(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a compact context-level retrieval diagnostics payload."""
    if not attempts:
        return {
            "status": "unknown",
            "partial_failure": False,
            "fallback_used": False,
            "attempt_count": 0,
            "degraded_reasons": [],
            "attempts": [],
        }

    partial_failure = any(bool(attempt.get("partial_failure")) for attempt in attempts)
    fallback_used = any(bool(attempt.get("fallback_used")) for attempt in attempts)
    degraded_reasons: list[str] = []
    seen_reasons: set[str] = set()
    for attempt in attempts:
        for reason in attempt.get("degraded_reasons", []):
            reason_text = str(reason)
            if reason_text in seen_reasons:
                continue
            seen_reasons.add(reason_text)
            degraded_reasons.append(reason_text)

    return {
        "status": "degraded" if partial_failure else "ok",
        "partial_failure": partial_failure,
        "fallback_used": fallback_used,
        "attempt_count": len(attempts),
        "degraded_reasons": degraded_reasons,
        "attempts": attempts[:8],
    }


def _error_diagnostics(query: str, exc: Exception) -> dict[str, Any]:
    reason = _format_error(exc)
    return {
        "query": query,
        "status": "degraded",
        "partial_failure": True,
        "fallback_used": False,
        "degraded_reasons": [f"context_search: {reason}"],
        "backends": [],
        "result_count": 0,
    }


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
        "recency": _first_present(item.get("updated_at"), item.get("created_at"), metadata.get("updated_at"), metadata.get("created_at")),
        "created_at": _first_present(item.get("created_at"), metadata.get("created_at")),
        "updated_at": _first_present(item.get("updated_at"), metadata.get("updated_at")),
        "quality_score": _first_present(item.get("quality_score"), metadata.get("quality_score")),
        "confidence": _first_present(item.get("confidence"), metadata.get("confidence")),
        "tags": item.get("tags") or metadata.get("tags") or [],
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
    limit = max(0, min(limit, _MAX_LIMIT))
    max_chars = max(0, min(max_chars, _MAX_CONTEXT_CHARS))
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
    diagnostic_attempts: list[dict[str, Any]] = []
    if gw is None:
        raw_results = []
    else:
        try:
            search_limit = max(limit * 3, _DEFAULT_LIMIT, 1)
            raw_results = gw.search(query=query, limit=search_limit) if query else []
            diagnostics = _search_diagnostics(gw, query)
            if diagnostics is not None:
                diagnostic_attempts.append(diagnostics)
            if not raw_results:
                seen_ids: set[str] = set()
                fallback_results: list[dict[str, Any]] = []
                for keyword in keywords:
                    keyword_results = gw.search(query=keyword, limit=search_limit)
                    diagnostics = _search_diagnostics(gw, keyword)
                    if diagnostics is not None:
                        diagnostic_attempts.append(diagnostics)
                    for result in keyword_results:
                        result_id = str(result.get("item_id") or result.get("id") or "")
                        if result_id in seen_ids:
                            continue
                        seen_ids.add(result_id)
                        fallback_results.append(result)
                        if len(fallback_results) >= search_limit:
                            break
                    if len(fallback_results) >= search_limit:
                        break
                raw_results = fallback_results
        except Exception as exc:
            raw_results = []
            partial_failure = True
            diagnostic_attempts.append(_error_diagnostics(query, exc))

    skipped: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    total_candidates = len(raw_results)
    if gw is not None:
        for index, result in enumerate(raw_results):
            rank_score = _score(index, total_candidates)
            item = _enrich(gw, result, rank_score)
            content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
            selection_score, components = _selection_score(
                item,
                keywords=keywords,
                rank_score=rank_score,
            )
            reason = _skip_reason(selection_score, item, content, components)
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue

            item["content"] = content
            item["score"] = selection_score
            item["score_components"] = components
            candidates.append(item)

    candidates.sort(
        key=lambda item: (
            float(item.get("score") or 0.0),
            str(item.get("updated_at") or item.get("created_at") or item.get("recency") or ""),
        ),
        reverse=True,
    )

    used_chars = 0
    results: list[dict[str, Any]] = []
    for item in candidates:
        if len(results) >= limit:
            skipped["limit"] = skipped.get("limit", 0) + 1
            continue

        content = str(item.get("content") or "")
        remaining = max_chars - used_chars
        if remaining <= 0:
            skipped["budget"] = skipped.get("budget", 0) + 1
            break

        memory_budget = min(remaining, _MAX_MEMORY_CHARS)
        if len(content) > memory_budget:
            content = content[: max(0, memory_budget - 3)].rstrip() + "..."
        used_chars += len(content)
        item["content"] = content
        results.append(item)

    retrieval_diagnostics = _combine_retrieval_diagnostics(diagnostic_attempts)
    partial_failure = partial_failure or bool(retrieval_diagnostics.get("partial_failure"))

    return {
        "provider": "mnemos",
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status": "degraded" if partial_failure else "ok",
        "partial_failure": partial_failure,
        "mode": "deterministic-v1",
        "host": host,
        "session_id": session_id,
        "repo_root": repo_root,
        "prompt": prompt,
        "query": query,
        "keywords": keywords,
        "count": len(results),
        "max_chars": max_chars,
        "used_chars": used_chars,
        "score_scale": SCORE_SCALE,
        "retrieval_diagnostics": retrieval_diagnostics,
        "selection": {
            "candidate_count": total_candidates,
            "selected_count": len(results),
            "skipped_count": sum(skipped.values()),
            "skipped_reasons": skipped,
            "max_memory_chars": _MAX_MEMORY_CHARS,
            "min_score": _MIN_SELECTION_SCORE,
        },
        "results": results,
    }


def render_context_block(payload: dict[str, Any]) -> str:
    """Render a bounded ``<mnemos-context>`` block."""
    promotion_block = render_promotion_block(repo_root=payload.get("repo_root"))
    if not payload.get("results"):
        return promotion_block

    selection = payload.get("selection") or {}
    retrieval = payload.get("retrieval_diagnostics") or {}
    attrs = {
        "mode": payload.get("mode", "deterministic-v1"),
        "host": payload.get("host") or "",
        "session-id": payload.get("session_id") or "",
        "query": payload.get("query") or "",
        "count": str(payload.get("count", 0)),
        "selected": str(selection.get("selected_count", payload.get("count", 0))),
        "skipped": str(selection.get("skipped_count", 0)),
        "retrieval-status": str(retrieval.get("status") or ""),
        "fallback-used": "true" if retrieval.get("fallback_used") else "false",
        "advisory": "true",
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


def _format_error(exc: Exception) -> str:
    """Return a compact, JSON-safe exception summary."""
    message = str(exc)
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__
