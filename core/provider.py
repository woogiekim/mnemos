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

    return {
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
