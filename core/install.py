from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

_WIKI_DIRS = [
    "wiki/global",
    "wiki/projects",
    "wiki/entities",
    "wiki/claims",
    "wiki/topics",
]

_AGENT_DIRS = [
    ".agent/runs",
    ".agent/sessions",
    ".agent/state",
    ".agent/reports",
    ".agent/tools",
    ".agent/workflows/hooks",
    ".agent/transient",
]

_DEFAULT_CONFIG: dict = {
    "repo_root": ".",
    "layers": {
        "transient": {
            "path_template": ".agent/transient/",
            "promotes_to": None,
            "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
        },
        "ephemeral": {
            "path_template": ".agent/runs/{run_id}/scratch/",
            "promotes_to": "working",
            "promotion": {"age_hours": 0.1, "access_count": 1, "quality_score": 0.3},
        },
        "working": {
            "path_template": ".agent/runs/{run_id}/working/",
            "promotes_to": "session",
            "promotion": {"age_hours": 1.0, "access_count": 2, "quality_score": 0.5},
        },
        "session": {
            "path_template": ".agent/sessions/{session_id}/",
            "promotes_to": "project",
            "promotion": {"age_hours": 4.0, "access_count": 3, "quality_score": 0.6},
        },
        "project": {
            "path_template": "wiki/projects/",
            "promotes_to": "global",
            "promotion": {"age_hours": 24.0, "access_count": 5, "quality_score": 0.7},
        },
        "global": {
            "path_template": "wiki/global/",
            "promotes_to": None,
            "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
        },
    },
    "forget": {"requires_archived": True},
    "archive": {
        "allowed_stages": ["stored", "retrieved", "used", "validated"],
    },
}

#: Transient layer definition used for policy migration (backfill on update).
_TRANSIENT_LAYER_DEF: dict = {
    "path_template": ".agent/transient/",
    "promotes_to": None,
    "promotion": {"age_hours": 0, "access_count": 0, "quality_score": 0.0},
}

_GITIGNORE_BLOCK = """\
# mnemos
.agent/runs/
.agent/sessions/
.agent/state/
"""

_AGENTS_MD_CONTENT = """\
# AGENTS.md — mnemos Agent Manifest

This file describes all agents registered in the mnemos memory operating system.

## Agent Registry

| Agent | Module | Role |
|---|---|---|
| Ingest | mnemos.agents.ingest | Reads raw/sources/, runs capture for each document |
| Writer | mnemos.agents.writer | Generates/rewrites wiki entries from captured memories |
| Linker | mnemos.agents.linker | Detects cross-references between wiki pages, writes backlinks |
| Contradiction | mnemos.agents.contradiction | Detects conflicting claims in wiki/claims/ |
| Lint | mnemos.agents.lint | Validates YAML front-matter and Markdown formatting |
| Query | mnemos.agents.query | Answers questions using search + memory-read |

## Agent Interfaces

All agents expose a `run()` method and may accept a `run_id` or `session_id`
parameter to scope ephemeral/working memory.

## Memory Layer Ownership

| Layer | Path | Managed By |
|---|---|---|
| Ephemeral | .agent/runs/{runId}/scratch/ | Ingest, Query |
| Working | .agent/runs/{runId}/working/ | Writer, Query |
| Session | .agent/sessions/{sessionId}/ | All agents |
| Project | wiki/projects/ | Writer, Linker |
| Global | wiki/global/ | Writer, Linker |
"""


def install(path: Path, home: Optional[Path] = None) -> None:
    """Scaffold a mnemos wiki repo at *path* and configure detected host adapters.

    Args:
        path: The wiki repository root to scaffold.
        home: The user's home directory.  Defaults to Path.home().
              Adapters are only run when their host is detected via
              ``adapter.is_present(home)``.
    """
    from core.adapters import ClaudeCodeAdapter, CursorAdapter

    path = path.resolve()
    if home is None:
        home = Path.home()

    # -- Wiki repo scaffold -----------------------------------------------
    for rel in _WIKI_DIRS + _AGENT_DIRS:
        (path / rel).mkdir(parents=True, exist_ok=True)

    config_path = path / "mnemos.yml"
    if not config_path.exists():
        with config_path.open("w", encoding="utf-8") as f:
            yaml.dump(_DEFAULT_CONFIG, f, default_flow_style=False, sort_keys=False)

    policy_path = path / "wiki" / "policy.yaml"
    if not policy_path.exists():
        policy_data = {
            "layers": _DEFAULT_CONFIG["layers"],
            "forget": _DEFAULT_CONFIG["forget"],
            "archive": _DEFAULT_CONFIG["archive"],
        }
        with policy_path.open("w", encoding="utf-8") as f:
            yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)

    agents_path = path / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(_AGENTS_MD_CONTENT, encoding="utf-8")

    gitignore_path = path / ".gitignore"
    existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
    if _GITIGNORE_BLOCK.strip() not in existing:
        with gitignore_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_GITIGNORE_BLOCK)

    # -- Shared: write MNEMOS_REPO_ROOT to ~/.zshrc -----------------------
    _install_zshrc(home, repo_root=str(path))

    # -- Host adapters (only run if host is present) ----------------------
    adapter_list = [ClaudeCodeAdapter(), CursorAdapter()]
    for adapter in adapter_list:
        if adapter.is_present(home):
            messages = adapter.install(home)
            for msg in messages:
                print(msg)


def migrate_policy_transient(repo_root: Path) -> bool:
    """Add the transient layer to an existing wiki/policy.yaml if absent.

    Called by ``mnemos update`` so that installations created before the
    transient layer was added receive the new layer definition automatically.

    Returns:
        True  — policy was updated (transient was missing and has been added).
        False — no change needed (transient already present or file absent).
    """
    policy_path = repo_root / "wiki" / "policy.yaml"
    if not policy_path.exists():
        return False

    with policy_path.open("r", encoding="utf-8") as f:
        policy_data = yaml.safe_load(f) or {}

    layers = policy_data.get("layers", {})
    if "transient" in layers:
        return False  # already present — nothing to do

    # Prepend transient so it appears first (shortest GC window reads cleanly).
    updated_layers: dict = {"transient": _TRANSIENT_LAYER_DEF}
    updated_layers.update(layers)
    policy_data["layers"] = updated_layers

    # Ensure the storage directory exists alongside the policy update.
    transient_dir = repo_root / ".agent" / "transient"
    transient_dir.mkdir(parents=True, exist_ok=True)

    with policy_path.open("w", encoding="utf-8") as f:
        yaml.dump(policy_data, f, default_flow_style=False, sort_keys=False)

    return True


def _install_zshrc(home: Path, repo_root: str) -> None:
    """Write export MNEMOS_REPO_ROOT=... to ~/.zshrc if not already present."""
    zshrc_path = home / ".zshrc"
    export_line = f'export MNEMOS_REPO_ROOT="{repo_root}"\n'
    existing = zshrc_path.read_text(encoding="utf-8") if zshrc_path.exists() else ""
    if "MNEMOS_REPO_ROOT" not in existing:
        with zshrc_path.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(f"# mnemos — repository root (added by install.py)\n")
            f.write(export_line)
