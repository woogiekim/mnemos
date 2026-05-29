# mnemos — LLM Wiki Memory OS

mnemos is a Global Memory Operating System (MemoryOS) that controls the complete
memory lifecycle for AI agents. It provides a single Memory Gateway entry point,
a Policy Engine that enforces lifecycle transitions, memory layers, and CLI tools
that agents use instead of accessing the filesystem directly.

## Foundational Direction

mnemos is governed by permanent architectural direction documents that define
its identity as an AI Memory Operating System and the memory layer for
persistent intelligence:

- [AI memory operating system philosophy](docs/ai-memory-operating-system-philosophy.md)
- [Memory lifecycle management direction](docs/memory-lifecycle-management-direction.md)
- [Context persistence as a first-class concern](docs/context-persistence-first-class-concern.md)

## Quick Start

### Option A — One-line remote install (recommended)

```bash
curl -s https://raw.githubusercontent.com/woogiekim/mnemos/main/install.sh | bash
```

This clones the repository to `~/.mnemos`, installs the package via pipx, and
scaffolds the wiki directory structure. `mnemos` is available in every new
terminal with no activation step required.

### Option B — Local clone

```bash
git clone https://github.com/woogiekim/mnemos.git
cd mnemos
./install.sh
```

`install.sh` handles everything: it installs pipx if needed, runs
`pipx install -e .`, and scaffolds the wiki directory structure. No manual
`pip install`, venv creation, or activation is needed.

Once installed, the `mnemos` CLI is available:

```bash
# Capture a memory
mnemos capture --layer global --content "The capital of France is Paris." --tag fact

# Ingest CLAUDE.md files (global and project-scoped)
mnemos ingest-claude-md --project-root .

# Search memories
mnemos search "capital France"

# Read a specific item
mnemos read <item-id>

# Promote to next layer
mnemos promote <item-id>

# Archive then forget
mnemos archive <item-id>
mnemos forget <item-id>
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
| `core/contracts.py` | Runtime-independent persistent memory protocol and retrieval contracts |
| `core/lifecycle.py` | Managed lifecycle policy for summarize, compress, promote, archive, and expire decisions |
| `core/compression.py` | Continuity-aware compression that preserves memory identity and relationships |
| `core/retrieval.py` | Operational retrieval ranking based on relevance, trust, workflow, recency, history, and quality |
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
| `agents/query.py` | QueryAgent — answers questions using search + memory-read |

## CLI Reference

| Command | Description |
|---|---|
| `mnemos install [PATH]` | Scaffold a wiki repo structure at PATH (default: current directory) |
| `mnemos capture` | Capture a new memory item into a target layer |
| `mnemos classify` | Classify/tag a captured item |
| `mnemos search` | Search across memory layers |
| `mnemos read` | Read a specific item by ID |
| `mnemos use` | Mark an item as "in use" |
| `mnemos edit` | Update item content |
| `mnemos promote` | Promote to next (or specified) layer |
| `mnemos demote` | Demote to a lower layer |
| `mnemos archive` | Soft-delete (retain content) |
| `mnemos forget` | Hard-delete (requires archived state; use `--force` to skip prompt) |
| `mnemos log` | Manually append or view audit log entries |
| `mnemos capabilities --json` | Print stable machine-readable provider features |
| `mnemos version --json` | Print version and compatibility metadata |
| `mnemos ingest-claude-md` | Discover and ingest CLAUDE.md files into memory |

## Stable Provider Contract

Host integrations should use the CLI/provider contract instead of reading
mnemos storage paths, SQLite FTS tables, or Markdown filenames directly.

```bash
mnemos capture --json --content "Architecture decision..." --layer project
mnemos search --fast --json --limit 5 "architecture decision"
mnemos read --json <item-id>
mnemos gc --json --dry-run
mnemos capabilities --json
mnemos version --json
```

Provider JSON search results include `id`, `content`, `summary`, `layer`,
`tags`, `provenance`, `recency`, optional `score`, and raw `metadata`.
When present, `score` is a stable relevance value from `0.0` to `1.0`;
higher means more relevant, and scores are relative within a single response.
No-result searches return `count: 0` and `results: []`. Commands that can
degrade return structured status fields so callers can distinguish supported,
unsupported, and unknown features via `mnemos capabilities --json`.
The `capabilities` object remains a backward-compatible boolean map. New
integrations should prefer `capability_status`, whose values are one of
`supported`, `unsupported`, or `unknown`.
If search cannot fully use its backend or index, JSON output remains parseable
with `status: "degraded"`, `partial_failure: true`, an empty or partial
`results` array, and `error.code` such as `timeout`, `locked`, or
`backend_error`. Missing `read --json` items return `status: "error"` with
`error.code: "not_found"` and a non-zero exit code. Locked vault or index
responses are retryable; policy and not-found errors are not.

`mnemos search --fast --json` is the stable fast-search entry point for host
integrations. Do not read `.agent/state/fts.db` directly; the database path,
schema, metadata format, and rank values are internal implementation details.
Existing direct FTS consumers should migrate by shelling out to
`mnemos search --fast --json --limit N "query"` and reading `results[]` from
stdout. Detect support with `mnemos capabilities --json` and check
`capabilities.fast_search` before using the fast-search contract.
Host callers should run provider commands with their own subprocess timeout and
treat timeout expiration as an unknown result, not as evidence of no memory.
Provider JSON never requires prompts or direct reads of `.agent/`, `wiki/`, or
SQLite internals.

Capability names are part of the stable provider contract for compatibility
checks:

| Capability | Stable behavior |
|---|---|
| `capture_json` | `mnemos capture --json` returns structured capture, duplicate, or error output |
| `search_json` | `mnemos search --json` returns structured search output |
| `fast_search` | `mnemos search --fast --json` is the supported low-latency search entry point |
| `search_scores` | Search results expose normalized relevance scores from `0.0` to `1.0` |
| `read_json` | `mnemos read --json` returns a structured item or `not_found` error |
| `gc_json` | `mnemos gc --json` returns structured dry-run and execution summaries |
| `host_install` | `mnemos install` manages supported host integration files |
| `safe_filenames` | Filesystem storage safely encodes unsafe item IDs while preserving logical IDs |
| `persistent_memory_protocol` | Runtime-independent contracts define capture, retrieval, lifecycle, trust, and backend boundaries |
| `memory_lifecycle_management` | Policy-driven lifecycle decisions cover summarize, compress, promote, archive, and expire |
| `continuity_compression` | Compression preserves memory IDs, relationships, and operational history within context budgets |
| `operational_retrieval` | Ranking optimizes for operational continuity, trust, workflow relevance, historical use, and semantic match |

These names should not be renamed or removed during the `1.x` provider
contract. If a behavior becomes unavailable, keep the name and change its
`capability_status` to `unsupported` or `unknown` so callers can branch safely.
agent-crew compatibility tracking is maintained in
[agent-crew#101](https://github.com/woogiekim/agent-crew/issues/101).

## Host Install Contract

mnemos owns memory behavior for supported AI hosts. `mnemos install` detects
available hosts and writes mnemos-managed marker blocks:

- Claude Code: `~/.claude/CLAUDE.md` plus `~/.claude/settings.json` hooks when
  those files exist.
- Cursor: `~/.cursor/rules` or `~/.cursor/rules.md` when present.

The managed blocks describe capture, search, read, and GC behavior independently
of agent-crew. When a host lacks hook support or expected config files, install
skips that host-specific surface without failing the repo scaffold.

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

The front-matter `id` is the durable logical identifier. It is not a filesystem
contract. The default filesystem store percent-encodes reserved filename
characters, so an ID such as `rule:input-language` is stored as a safe filename
such as `rule%3Ainput-language.md` while preserving `id: rule:input-language`
in frontmatter. Legacy raw-ID filenames remain readable by frontmatter lookup.
Use `mnemos migrate --safe-filenames` to rename legacy unsafe files. The
migration uses `git mv` for tracked files when possible; add `--dry-run` to
preview the migration.

## Configuration

Set `MNEMOS_REPO_ROOT` to point to your repo root. Default is the current directory.

Optional vector backend:
- `MNEMOS_VECTOR_BACKEND=qdrant` + `MNEMOS_QDRANT_URL=http://localhost:6333`
- `MNEMOS_VECTOR_BACKEND=chroma` + `MNEMOS_CHROMA_PATH=.agent/state/chroma`

Install with vector extras:

```bash
pip install -e ".[vector]"
```

## Remote sync

mnemos ships a git-backed remote sync layer that keeps the wiki tree in lockstep
with a single remote git repository (`mnemos sync init --remote <url>`, pull on
capture, commit on write, push after commit, manual `mnemos sync continue` on
conflict).  See the [remote sync operator guide](docs/remote-sync.md) for
setup, the normal pull → commit → push flow, the conflict resolution path,
and known limits.

For explicit, portable snapshots (the disaster-recovery / multi-host hand-off
companion to remote sync), see the
[backup & restore operator guide](docs/backup-restore.md) — covers
`mnemos backup` / `mnemos restore`, the dual-track model, and the manifest
schema.

## Releasing

For cutting a release — the SemVer policy across the CLI + memory store + host
adapters, the tag/changelog/build/publish workflow, the rollback strategy, and a
captured dry-run rehearsal — see the
[release workflow & versioning policy guide](docs/release-workflow.md). The
dry-run-by-default helper [`scripts/release.sh`](scripts/release.sh) runs the
pre-flight checks and prints the release commands.

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
