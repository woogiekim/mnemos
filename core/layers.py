"""Shared layer-to-path mapping used by store and search modules."""
from __future__ import annotations

LAYER_STATIC_PATHS: dict[str, str] = {
    "project": "wiki/projects",
    "global": "wiki/global",
    "entities": "wiki/entities",
    "claims": "wiki/claims",
    "topics": "wiki/topics",
}

#: Path (relative to repo root) for the transient layer.
#:
#: The transient layer is a flat directory for protocol-test and throwaway
#: captures.  Unlike ephemeral/working it has no run_id sub-namespace because
#: it is not scoped to a single agent run — it is shared across all processes
#: and cleaned up by GC on a short 1-hour staleness window.
TRANSIENT_PATH: str = ".agent/transient"
