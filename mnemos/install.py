from __future__ import annotations

from pathlib import Path

import yaml

_LAYERS = ["ephemeral", "working", "session", "project", "global"]

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
]

_DEFAULT_CONFIG: dict = {
    "repo_root": ".",
    "layers": {
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
| Ingest | mnemos.agents.ingest | Reads raw/sources/, runs memory-capture for each document |
| Writer | mnemos.agents.writer | Generates/rewrites wiki entries from captured memories |
| Linker | mnemos.agents.linker | Detects cross-references between wiki pages, writes backlinks |
| Contradiction | mnemos.agents.contradiction | Detects conflicting claims in wiki/claims/ |
| Lint | mnemos.agents.lint | Validates YAML front-matter and Markdown formatting |
| Query | mnemos.agents.query | Answers questions using memory-search + memory-read |

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


def install(path: Path) -> None:
    path = path.resolve()

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
