"""Stable provider contract helpers for host integrations."""
from __future__ import annotations

from importlib import metadata
from typing import Any


PROVIDER_CONTRACT_VERSION = "1.0"
CAPABILITY_STATUS_VALUES = ["supported", "unsupported", "unknown"]

CAPABILITIES: dict[str, bool | str] = {
    "capture_json": True,
    "search_json": True,
    "fast_search": True,
    "recall_v1": True,
    "recall_read_only": True,
    "retrieval_score": True,
    "project_scope_filter": True,
    "context_json": True,
    "context_render": True,
    "transcript_capture_json": True,
    "search_scores": True,
    "read_json": True,
    "gc_json": True,
    "host_install": True,
    "safe_filenames": True,
    "autonomous_capture": "supported",
    "context_injection": "supported",
    "daemon_runtime": "supported",
    "persistent_memory_protocol": "supported",
    "memory_lifecycle_management": "supported",
    "continuity_compression": "supported",
    "operational_retrieval": "supported",
    "lifecycle_execution": "supported",
    "operational_metrics": "supported",
    "memory_recovery": "supported",
    "operational_evidence": "supported",
    "health_validation": "supported",
    "autonomous_health_maintenance": "supported",
    "autonomous_memory_recovery": "supported",
    "managed_compression_jobs": "supported",
    "empirical_metric_calibration": "supported",
    "retrieval_backend_health": "supported",
    "retrieval_degradation_evidence": "supported",
    "memory_os_readiness_audit": "supported",
    "source_document_ingestion": "supported",
    "source_code_scan": "supported",
    "project_context_section_capture": "supported",
    "project_context_recall": "supported",
    "project_context_freshness_audit": "supported",
}


def _capability_status(value: bool | str) -> str:
    """Normalize legacy boolean capabilities into the tri-state contract."""
    if value is True:
        return "supported"
    if value is False:
        return "unsupported"
    if value in CAPABILITY_STATUS_VALUES:
        return value
    return "unknown"


CAPABILITY_STATUS: dict[str, str] = {
    name: _capability_status(value)
    for name, value in CAPABILITIES.items()
}

CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "capture_json": "mnemos capture --json emits a stable provider-contract JSON payload.",
    "search_json": "mnemos search --json emits stable JSON with query metadata and results.",
    "fast_search": "mnemos search --fast --json is the stable low-latency search entry point.",
    "recall_v1": "mnemos recall --json --request-file emits the structured read-only recall provider contract.",
    "recall_read_only": "Recall provider requests must declare read_only=true and use MemoryGateway.recall without mutating memory items.",
    "retrieval_score": "Recall results expose operational retrieval_score when the core supplies a real retrieval score.",
    "project_scope_filter": "Recall requests can filter by project_id and project_root_hash.",
    "context_json": "mnemos context --json emits bounded deterministic host-injectable context.",
    "context_render": "mnemos context --render emits bounded <mnemos-context> blocks.",
    "transcript_capture_json": "mnemos capture-transcript --json extracts and captures deterministic transcript insights.",
    "search_scores": "Search JSON results include normalized 0.0-1.0 relevance scores.",
    "read_json": "mnemos read --json emits a stable item payload or structured not_found error.",
    "gc_json": "mnemos gc --json emits structured dry-run and execution summaries.",
    "host_install": "mnemos install manages supported host integration files.",
    "safe_filenames": "Storage percent-encodes unsafe IDs while preserving logical item IDs.",
    "autonomous_capture": "Transcript capture can run from host lifecycle hooks without AI-authored capture prompts.",
    "context_injection": "Host adapters can inject deterministic context before prompt handling.",
    "daemon_runtime": "mnemos daemon run/status/install/uninstall manages autonomous background maintenance.",
    "persistent_memory_protocol": "Execution systems can depend on runtime-independent persistent memory contracts.",
    "memory_lifecycle_management": "Memory lifecycle policy supports summarize, compress, promote, archive, and expire decisions.",
    "continuity_compression": "Context compression preserves memory identity, relationships, and operational continuity under a budget.",
    "operational_retrieval": "Retrieval ranking combines semantic relevance, workflow relevance, trust, recency, history, and quality.",
    "lifecycle_execution": "mnemos lifecycle-run plans and applies managed lifecycle transitions with structured reports.",
    "operational_metrics": "mnemos memory-metrics reports continuity, relevance, history, compression, lifecycle, and stability scores.",
    "memory_recovery": "mnemos recover detects and repairs recoverable metadata/index issues without hard-deleting memory.",
    "operational_evidence": "Lifecycle, metrics, and validation commands can persist durable evidence under .agent/reports/memory-os.",
    "health_validation": "mnemos memory-validate evaluates Memory OS health against calibrated operational score gates.",
    "autonomous_health_maintenance": "mnemos bg-check --memory-os records lifecycle, metrics, and health evidence from the autonomous maintenance path.",
    "autonomous_memory_recovery": "mnemos bg-check --memory-os --memory-os-recover repairs metadata and reindexes before health scoring.",
    "managed_compression_jobs": "mnemos memory-compress builds durable continuity page artifacts from operational memory.",
    "empirical_metric_calibration": "mnemos memory-calibrate derives and persists health validation baselines from metric history.",
    "retrieval_backend_health": "mnemos memory-backends reports FTS, vector, and fallback retrieval health.",
    "retrieval_degradation_evidence": "Search and Memory OS evidence expose backend degradation, vector availability, and fallback use.",
    "memory_os_readiness_audit": "mnemos memory-readiness aggregates metrics, validation, backend health, evidence freshness, and remediation gaps.",
    "source_document_ingestion": "mnemos ingest-docs captures document folders as source-backed memory items.",
    "source_code_scan": "mnemos scan-code captures lightweight code-structure memory from source files.",
    "project_context_section_capture": "mnemos project-context capture indexes durable markdown sections with stable source metadata.",
    "project_context_recall": "mnemos project-context recall returns structured project-scoped section recall records and traces.",
    "project_context_freshness_audit": "mnemos project-context audit reports stale or missing indexed sections relative to markdown sources.",
}

HOST_CAPABILITY_STATUS: dict[str, dict[str, str]] = {
    "claude": {
        "autonomous_capture": "supported",
        "context_injection": "supported",
        "daemon_runtime": "supported",
    },
    "claude-code": {
        "autonomous_capture": "supported",
        "context_injection": "supported",
        "daemon_runtime": "supported",
    },
    "cursor": {
        "autonomous_capture": "unsupported",
        "context_injection": "unknown",
        "daemon_runtime": "supported",
    },
    "codex": {
        "autonomous_capture": "unsupported",
        "context_injection": "unknown",
        "daemon_runtime": "supported",
    },
}

SCORE_SCALE = {
    "min": 0.0,
    "max": 1.0,
    "direction": "higher_is_more_relevant",
    "semantics": "Relative relevance within this response only; derived from returned result order.",
}

RECALL_SCORE_SEMANTICS = {
    "rank_score": "Stable result-order score within this response; derived only from final result rank.",
    "retrieval_score": "Operational retrieval score from Recall Core when available; null when unsupported or unavailable.",
    "context_score": "Final selected-context score after deduplication and budget selection.",
}


def package_version() -> str:
    """Return the installed package version, or ``unknown`` in editable tests."""
    try:
        return metadata.version("mnemos")
    except metadata.PackageNotFoundError:
        return "unknown"


def capabilities_payload() -> dict[str, Any]:
    """Return the machine-readable capability document."""
    return {
        "provider": "mnemos",
        "version": package_version(),
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status_values": CAPABILITY_STATUS_VALUES,
        "capabilities": dict(CAPABILITIES),
        "capability_status": dict(CAPABILITY_STATUS),
        "capability_descriptions": dict(CAPABILITY_DESCRIPTIONS),
        "host_capability_status": dict(HOST_CAPABILITY_STATUS),
        "recall_scores": dict(RECALL_SCORE_SEMANTICS),
    }


def version_payload() -> dict[str, Any]:
    """Return the machine-readable version document."""
    return {
        "provider": "mnemos",
        "version": package_version(),
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status_values": CAPABILITY_STATUS_VALUES,
        "capabilities": dict(CAPABILITIES),
        "capability_status": dict(CAPABILITY_STATUS),
        "recall_scores": dict(RECALL_SCORE_SEMANTICS),
    }


def provider_error_payload(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> dict[str, Any]:
    """Return the stable provider error envelope for JSON command callers."""
    return {
        "provider": "mnemos",
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def provider_error_from_exception(exc: Exception) -> dict[str, Any]:
    """Classify common backend failures into stable provider error codes."""
    message = str(exc)
    lowered = message.lower()

    if isinstance(exc, TimeoutError) or "timeout" in lowered or "timed out" in lowered:
        return provider_error_payload("timeout", message, retryable=True)
    if "locked" in lowered:
        return provider_error_payload("locked", message, retryable=True)

    return provider_error_payload("backend_error", message, retryable=True)


def memory_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored memory item for provider API consumers."""
    return {
        "id": item.get("id"),
        "content": item.get("content", ""),
        "summary": item.get("summary"),
        "layer": item.get("layer"),
        "tags": item.get("tags") or [],
        "provenance": item.get("provenance") or {
            "source": item.get("source"),
            "path": item.get("_path"),
        },
        "recency": item.get("created_at") or item.get("updated_at"),
        "metadata": {
            k: v
            for k, v in item.items()
            if k not in {"content", "_path", "id", "layer", "tags", "summary"}
        },
    }


def search_result_payload(
    result: dict[str, Any],
    *,
    score: float | None = None,
) -> dict[str, Any]:
    """Normalize a search result for provider API consumers."""
    metadata_payload = result.get("metadata") or {}
    payload = {
        "id": result.get("item_id") or result.get("id"),
        "content": result.get("content", ""),
        "summary": result.get("summary"),
        "layer": metadata_payload.get("layer") or result.get("layer"),
        "tags": metadata_payload.get("tags") or result.get("tags") or [],
        "provenance": result.get("provenance") or {
            "source": result.get("source"),
            "path": metadata_payload.get("path"),
        },
        "recency": result.get("recency") or metadata_payload.get("created_at"),
        "metadata": metadata_payload,
    }
    if score is not None:
        payload["score"] = round(score, 6)
    return payload


def search_payload(
    *,
    query: str,
    results: list[dict[str, Any]],
    mode: str,
    partial_failure: bool = False,
    retrieval_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable JSON payload for provider search commands.

    Public scores use a documented 0.0-1.0 scale and are intentionally derived
    from final result ordering instead of backend-native rank values.
    """
    count = len(results)
    if count == 0:
        scores: list[float | None] = []
    elif count == 1:
        scores = [1.0]
    else:
        scores = [1.0 - (idx / (count - 1)) for idx in range(count)]

    if retrieval_diagnostics:
        partial_failure = partial_failure or bool(retrieval_diagnostics.get("partial_failure"))

    payload = {
        "status": "degraded" if partial_failure else "ok",
        "query": query,
        "count": count,
        "mode": mode,
        "partial_failure": partial_failure,
        "score_scale": SCORE_SCALE,
        "results": [
            search_result_payload(result, score=score)
            for result, score in zip(results, scores)
        ],
    }
    if retrieval_diagnostics:
        payload["retrieval_diagnostics"] = retrieval_diagnostics
    return payload


def recall_response_payload(
    *,
    request_id: str | None,
    report: Any,
    duration_ms: int,
) -> dict[str, Any]:
    """Return the stable JSON payload for ``mnemos recall``."""
    selected_ids = [str(getattr(item, "id", "")) for item in getattr(report, "selected", ())]
    selected_id_set = set(selected_ids)
    results = list(getattr(report, "candidates", ()))
    count = len(results)
    degraded_reasons = _recall_degraded_reasons(report)
    partial_failure = bool(degraded_reasons)
    diagnostics = _recall_diagnostics(report, duration_ms=duration_ms, degraded_reasons=degraded_reasons)

    return {
        "schema_version": "mnemos.recall.response.v1",
        "provider": "mnemos",
        "request_id": request_id,
        "status": "degraded" if partial_failure else "ok",
        "partial_failure": partial_failure,
        "results": [
            _recall_result_payload(
                item,
                index=index,
                count=count,
                selected=item_id in selected_id_set,
            )
            for index, item in enumerate(results)
            for item_id in [str(getattr(item, "id", ""))]
        ],
        "selected_ids": selected_ids,
        "diagnostics": diagnostics,
    }


def recall_error_payload(
    *,
    request_id: str | None,
    code: str,
    message: str,
    retryable: bool = False,
    duration_ms: int = 0,
) -> dict[str, Any]:
    """Return the stable JSON error payload for ``mnemos recall``."""
    return {
        "schema_version": "mnemos.recall.response.v1",
        "provider": "mnemos",
        "request_id": request_id,
        "status": "error",
        "partial_failure": False,
        "results": [],
        "selected_ids": [],
        "diagnostics": {
            "backends": [],
            "fallback_used": False,
            "duration_ms": duration_ms,
            "degraded_reasons": [],
        },
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def _recall_result_payload(
    item: Any,
    *,
    index: int,
    count: int,
    selected: bool,
) -> dict[str, Any]:
    retrieval_score = _optional_float(getattr(item, "score", None))
    context_score = retrieval_score if selected else None
    return {
        "memory_id": getattr(item, "id", None),
        "content": getattr(item, "content", ""),
        "summary": getattr(item, "summary", None),
        "layer": getattr(item, "layer", None),
        "semantic_status": getattr(item, "semantic_status", None),
        "tags": list(getattr(item, "tags", ()) or []),
        "record_type": getattr(item, "record_type", None),
        "task_shape": getattr(item, "task_shape", None),
        "project_id": getattr(item, "project_id", None),
        "project_root_hash": getattr(item, "project_root_hash", None),
        "provenance": getattr(item, "provenance", None) or {},
        "created_at": getattr(item, "created_at", None),
        "updated_at": getattr(item, "updated_at", None),
        "rank_score": _rank_score(index, count),
        "retrieval_score": retrieval_score,
        "context_score": context_score,
        "score_components": getattr(item, "score_components", None) or {},
        "match_reasons": list(getattr(item, "matched_queries", ()) or []),
        "supersedes": list(getattr(item, "supersedes", ()) or []),
        "superseded_by": getattr(item, "superseded_by", None),
    }


def _rank_score(index: int, count: int) -> float:
    if count <= 1:
        return 1.0 if count == 1 else 0.0
    return round(1.0 - (index / (count - 1)), 6)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _recall_degraded_reasons(report: Any) -> list[str]:
    reasons: list[str] = []
    seen: set[str] = set()
    diagnostics = getattr(report, "diagnostics", {}) or {}
    for attempt in diagnostics.get("attempts", []) or []:
        if attempt.get("partial_failure") and not attempt.get("degraded_reasons"):
            _append_unique(reasons, seen, "retrieval_backend_partial_failure")
        if attempt.get("fallback_used"):
            _append_unique(reasons, seen, "retrieval_fallback_used")
        for reason in attempt.get("degraded_reasons", []) or []:
            _append_unique(reasons, seen, str(reason))

    for item in getattr(report, "candidates", ()) or ():
        if _optional_float(getattr(item, "score", None)) is None:
            _append_unique(reasons, seen, "retrieval_score_unavailable")
            break
    return reasons


def _recall_diagnostics(
    report: Any,
    *,
    duration_ms: int,
    degraded_reasons: list[str],
) -> dict[str, Any]:
    backends: list[dict[str, Any]] = []
    fallback_used = False
    seen_backends: set[tuple[str, str]] = set()
    diagnostics = getattr(report, "diagnostics", {}) or {}
    for attempt in diagnostics.get("attempts", []) or []:
        fallback_used = fallback_used or bool(attempt.get("fallback_used"))
        for backend in attempt.get("backends", []) or []:
            key = (str(backend.get("name")), str(backend.get("status")))
            if key in seen_backends:
                continue
            seen_backends.add(key)
            backends.append(dict(backend))

    return {
        "backends": backends,
        "fallback_used": fallback_used,
        "duration_ms": duration_ms,
        "degraded_reasons": degraded_reasons,
    }


def _append_unique(values: list[str], seen: set[str], value: str) -> None:
    if value in seen:
        return
    seen.add(value)
    values.append(value)


def error_payload(
    *,
    code: str,
    message: str,
    retryable: bool,
    status: str = "error",
) -> dict[str, Any]:
    """Return a stable provider-contract error payload."""
    return {
        "provider": "mnemos",
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "status": status,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
