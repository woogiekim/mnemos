"""Shared layer-to-path mapping used by store and search modules."""
from __future__ import annotations

LAYER_STATIC_PATHS: dict[str, str] = {
    "project": "wiki/projects",
    "global": "wiki/global",
    "entities": "wiki/entities",
    "claims": "wiki/claims",
    "topics": "wiki/topics",
}
