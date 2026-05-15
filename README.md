# mnemos — LLM Wiki Memory OS

mnemos is a Global Memory Operating System (MemoryOS) that controls the complete
memory lifecycle for all AI agents. It provides a single Memory Gateway entry point,
a Policy Engine that enforces lifecycle transitions, five memory layers, and a set
of CLI tools that agents use instead of accessing the filesystem directly.

## Install

```bash
pip install -e .
# With optional vector backends:
pip install -e ".[vector]"
```

## Quick Start

```bash
# Capture a memory
mnemos memory-capture --layer global --content "The capital of France is Paris." --tag fact

# Search memories
mnemos memory-search "capital France"

# Read a specific item
mnemos memory-read <item-id>

# Promote to next layer
mnemos memory-promote <item-id>

# Archive then forget
mnemos memory-archive <item-id>
mnemos memory-forget <item-id>
```

## Memory Layers

| Layer | Path | Lifetime | Promotes To |
|---|---|---|---|
| Ephemeral | `.agent/runs/{runId}/scratch/` | End of run | Working |
| Working | `.agent/runs/{runId}/working/` | End of session | Session |
| Session | `.agent/sessions/{sessionId}/` | End of session | Project |
| Project | `wiki/projects/` | Indefinite | Global |
| Global | `wiki/global/` | Permanent | — |

## CLI Reference

| Command | Description |
|---|---|
| `mnemos memory-capture` | Capture a new memory item |
| `mnemos memory-classify` | Classify/tag a captured item |
| `mnemos memory-search` | Search across layers |
| `mnemos memory-read` | Read a specific item by ID |
| `mnemos memory-use` | Mark item as "in use" |
| `mnemos memory-update` | Update item content |
| `mnemos memory-promote` | Promote to next layer |
| `mnemos memory-demote` | Demote to lower layer |
| `mnemos memory-archive` | Soft-delete (retain content) |
| `mnemos memory-forget` | Hard-delete (requires archived) |
| `mnemos memory-log` | Append to audit log |

## Memory Item Format

Every memory item is a Markdown file with YAML front-matter:

```yaml
---
id: 550e8400-e29b-41d4-a716-446655440000
layer: global
stage: stored
created_at: 2026-05-15T08:44:58Z
access_count: 0
quality_score: 0.8
tags: []
---
Content goes here.
```

## Configuration

Set `MNEMOS_REPO_ROOT` to point to your repo root. Default is the current directory.

Optional vector backend:
- `MNEMOS_VECTOR_BACKEND=qdrant` + `MNEMOS_QDRANT_URL=http://localhost:6333`
- `MNEMOS_VECTOR_BACKEND=chroma` + `MNEMOS_CHROMA_PATH=.agent/state/chroma`

## Architecture

- `mnemos/gateway.py` — Memory Gateway (single entry point)
- `mnemos/policy.py` — Policy Engine (lifecycle validation)
- `mnemos/store.py` — Filesystem Store (read/write memory items)
- `mnemos/search.py` — Search Middleware (FTS → vector → grep)
- `mnemos/fts.py` — SQLite FTS5 index
- `mnemos/vector.py` — Vector search stub (graceful fallback)
- `mnemos/hooks.py` — Hook dispatcher
- `mnemos/log.py` — Audit logger
- `mnemos/cli.py` — Click CLI
- `mnemos/agents/` — Ingest, Writer, Linker, Contradiction, Lint, Query agents
