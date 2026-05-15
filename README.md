# mnemos — LLM Wiki Memory OS

mnemos is a Global Memory Operating System (MemoryOS) that controls the complete
memory lifecycle for AI agents. It provides a single Memory Gateway entry point,
a Policy Engine that enforces lifecycle transitions, memory layers, and CLI tools
that agents use instead of accessing the filesystem directly.

## Quick Start

```bash
git clone <repo-url> mnemos
cd mnemos
./install.sh
source .venv/bin/activate
```

`install.sh` handles everything: it auto-creates `.venv/`, upgrades pip, runs
`pip install -e .`, and scaffolds the wiki directory structure. No manual
`pip install` or venv setup is needed.

Once installed, the `mnemos` CLI is available:

```bash
# Capture a memory
mnemos memory-capture --layer global --content "The capital of France is Paris." --tag fact

# Ingest CLAUDE.md files (global and project-scoped)
mnemos memory-ingest-claude-md --project-root .

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

## Directory Structure

```
mnemos/
├── core/               # Python package — gateway, store, policy, fts, search,
│                       #   hooks, log, install, layers, vector, cli
├── agents/             # Agent implementations — ingest, scanner, query,
│                       #   linker, writer, lint, contradiction
├── tests/              # Test suite
├── install.sh          # One-touch setup
└── pyproject.toml
```

## Architecture

### Core (`core/`)

| Module | Role |
|---|---|
| `core/gateway.py` | Memory Gateway — single entry point for all memory lifecycle operations |
| `core/policy.py` | Policy Engine — enforces lifecycle transitions and promotion eligibility |
| `core/store.py` | Filesystem Store — read/write memory items as Markdown with YAML front-matter |
| `core/search.py` | Search Middleware — three-stage pipeline: FTS5 → vector → pathlib grep |
| `core/fts.py` | SQLite FTS5 index for full-text search |
| `core/vector.py` | Optional vector search backend (Qdrant / Chroma; graceful fallback) |
| `core/layers.py` | Shared layer-to-path mapping used by store and search modules |
| `core/hooks.py` | Hook Dispatcher — fires shell scripts on post-capture, post-promote, post-archive, post-forget |
| `core/log.py` | Audit Logger — append-only log written to `wiki/log.md` and `wiki/log.jsonl` |
| `core/install.py` | Repo scaffolder — creates wiki dirs, `.agent/` dirs, `mnemos.yml`, and `wiki/policy.yaml` |
| `core/cli.py` | Click CLI — all `memory-*` subcommands |

### Agents (`agents/`)

| Module | Role |
|---|---|
| `agents/scanner.py` | ClaudeMdScanner — discovers `~/.claude/CLAUDE.md` and `<project>/CLAUDE.md` for ingestion |
| `agents/ingest.py` | IngestAgent — reads raw/sources/ (or explicit file list) and captures each document into memory |
| `agents/writer.py` | WriterAgent — generates or rewrites wiki entries from captured memories |
| `agents/linker.py` | LinkerAgent — detects `[[wikilink]]` cross-references and adds backlinks |
| `agents/contradiction.py` | ContradictionAgent — detects conflicting claims in `wiki/claims/`, writes `.agent/reports/contradictions.md` |
| `agents/lint.py` | LintAgent — validates YAML front-matter and detects broken wikilinks and orphan pages |
| `agents/query.py` | QueryAgent — answers questions using memory-search + memory-read |

## CLI Reference

| Command | Description |
|---|---|
| `mnemos install [PATH]` | Scaffold a wiki repo structure at PATH (default: current directory) |
| `mnemos memory-capture` | Capture a new memory item into a target layer |
| `mnemos memory-classify` | Classify/tag a captured item |
| `mnemos memory-search` | Search across memory layers |
| `mnemos memory-read` | Read a specific item by ID |
| `mnemos memory-use` | Mark an item as "in use" |
| `mnemos memory-update` | Update item content |
| `mnemos memory-promote` | Promote to next (or specified) layer |
| `mnemos memory-demote` | Demote to a lower layer |
| `mnemos memory-archive` | Soft-delete (retain content) |
| `mnemos memory-forget` | Hard-delete (requires archived state; use `--force` to skip prompt) |
| `mnemos memory-log` | Manually append an entry to the audit log |
| `mnemos memory-ingest-claude-md` | Discover and ingest CLAUDE.md files into memory |

## Memory Lifecycle

Memory items progress through lifecycle stages managed by the Policy Engine:

```
RawMemoryArtifact → ExtractedMemory → PromotedMemory → ArchivedMemory
```

### Memory Layers

| Layer | Path | Lifetime | Promotes To |
|---|---|---|---|
| Ephemeral | `.agent/runs/{runId}/scratch/` | End of run | Working |
| Working | `.agent/runs/{runId}/working/` | End of session | Session |
| Session | `.agent/sessions/{sessionId}/` | End of session | Project |
| Project | `wiki/projects/` | Indefinite | Global |
| Global | `wiki/global/` | Permanent | — |

Static wiki layers hold structured knowledge:

| Layer | Path | Purpose |
|---|---|---|
| Entities | `wiki/entities/` | Named entities (people, places, things) |
| Claims | `wiki/claims/` | Factual claims for contradiction detection |
| Topics | `wiki/topics/` | Topic-level summaries |

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

Install with vector extras:

```bash
pip install -e ".[vector]"
```

## Wiki Repo Structure

After running `./install.sh` (or `mnemos install .`):

```
wiki/
  global/          ← permanent global memories
  projects/        ← project-scoped memories
  entities/        ← named entities
  claims/          ← factual claims
  topics/          ← topic summaries
  log.md           ← human-readable audit log
  log.jsonl        ← machine-readable audit log (one JSON object per line)
  policy.yaml      ← lifecycle policy configuration

.agent/
  runs/            ← ephemeral and working memory (scoped by run ID)
  sessions/        ← session memory (scoped by session ID)
  state/           ← FTS index (fts.db) and other runtime state
  reports/         ← agent output reports (contradictions.md, lint.md)
  workflows/hooks/ ← hook scripts fired after memory mutations

mnemos.yml         ← main configuration file
AGENTS.md          ← agent manifest
```
