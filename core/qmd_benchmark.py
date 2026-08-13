"""Deterministic metrics for labelled QMD retrieval evaluation fixtures."""
from __future__ import annotations

import math
import re
from typing import Any


class BenchmarkFixtureError(ValueError):
    """Raised when a labelled retrieval fixture cannot support valid metrics."""


def token_overlap(query: str, content: str) -> float:
    """Return query-token coverage shared with content for fixture auditing."""
    query_terms = set(_tokens(query))
    if not query_terms:
        return 0.0
    content_terms = set(_tokens(content))
    return len(query_terms & content_terms) / len(query_terms)


def evaluate_fixture(
    payload: dict[str, Any],
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """Compute Recall@K, MRR, and nearest-rank p95 for two result sets."""
    if not isinstance(payload, dict):
        raise BenchmarkFixtureError("benchmark fixture must be an object")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkFixtureError("benchmark fixture must contain cases")
    top_k = int(top_k)
    if top_k <= 0:
        raise BenchmarkFixtureError("top_k must be positive")

    case_ids = [str(case.get("case_id") or "") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(cases) or any(not case_id for case_id in case_ids):
        raise BenchmarkFixtureError("every benchmark case requires case_id")
    if len(set(case_ids)) != len(case_ids):
        raise BenchmarkFixtureError("benchmark case_id values must be unique")

    return {
        "schema_version": 1,
        "evidence_type": str(payload.get("evidence_type") or "unknown"),
        "case_count": len(cases),
        "top_k": top_k,
        "lexical": _evaluate_backend(cases, "lexical", top_k),
        "qmd": _evaluate_backend(cases, "qmd", top_k),
        "claim_boundary": (
            "Synthetic fixture evidence; not a live QMD or model benchmark."
        ),
    }


def _evaluate_backend(
    cases: list[dict[str, Any]],
    backend: str,
    top_k: int,
) -> dict[str, float]:
    hits = 0
    reciprocal_rank = 0.0
    latencies: list[float] = []
    for case in cases:
        relevant_id = str(case.get("relevant_id") or "")
        raw_results = case.get(f"{backend}_results")
        if not relevant_id or not isinstance(raw_results, list):
            raise BenchmarkFixtureError(
                f"case {case.get('case_id')} has invalid {backend} ranking"
            )
        results = [str(item) for item in raw_results]
        try:
            rank = results.index(relevant_id) + 1
        except ValueError:
            rank = None
        if rank is not None:
            reciprocal_rank += 1.0 / rank
            if rank <= top_k:
                hits += 1

        latency = _non_negative_float(
            case.get(f"{backend}_latency_ms"),
            case_id=str(case.get("case_id")),
            backend=backend,
        )
        latencies.append(latency)

    count = len(cases)
    return {
        f"recall_at_{top_k}": round(hits / count, 6),
        "mrr": round(reciprocal_rank / count, 6),
        "p95_latency_ms": round(_nearest_rank_percentile(latencies, 0.95), 6),
    }


def _non_negative_float(value: Any, *, case_id: str, backend: str) -> float:
    if isinstance(value, bool):
        raise BenchmarkFixtureError(f"case {case_id} has invalid {backend} latency")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BenchmarkFixtureError(
            f"case {case_id} has invalid {backend} latency"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise BenchmarkFixtureError(f"case {case_id} has invalid {backend} latency")
    return parsed


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9가-힣_./-]{2,}", text)]
