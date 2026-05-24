"""Stable provider contract helpers for host integrations."""
from __future__ import annotations

from importlib import metadata
from typing import Any


PROVIDER_CONTRACT_VERSION = "1.0"

CAPABILITIES: dict[str, bool | str] = {
    "capture_json": True,
    "search_json": True,
    "fast_search": True,
    "search_scores": True,
    "read_json": True,
    "gc_json": True,
    "host_install": True,
    "safe_filenames": True,
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
        "status_values": ["supported", "unsupported", "unknown"],
        "capabilities": CAPABILITIES,
    }


def version_payload() -> dict[str, Any]:
    """Return the machine-readable version document."""
    return {
        "provider": "mnemos",
        "version": package_version(),
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "capabilities": CAPABILITIES,
    }


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


def search_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize a search result for provider API consumers."""
    metadata_payload = result.get("metadata") or {}
    return {
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
        "score": result.get("score"),
        "metadata": metadata_payload,
    }
