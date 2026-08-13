# mnemos — local Memory OS for AI coding agents

mnemos is a local, host-installable memory layer for AI coding agents. It gives
Claude Code, Cursor, and other CLI/provider-contract hosts a durable way to
capture decisions, retrieve them later, manage lifecycle policy, inspect what
was stored, and move memory across machines without agents reading private
storage internals directly.

In practical terms: install mnemos in a repo, capture project or global
decisions through the `mnemos` CLI, and let supported hosts call the stable
JSON/provider commands for search, read, context injection, lifecycle checks,
backup, restore, and sync. "Memory OS" here means the memory store has a
gateway, policy layer, lifecycle stages, inspection surfaces, and host adapters
instead of being a loose folder of notes.

mnemos is for developers who want AI coding sessions to remember durable
project facts without turning those facts into prompt boilerplate, ad hoc
Markdown scraping, or hidden vendor state.

## Foundational Direction

mnemos is governed by permanent architectural direction documents that define
its identity as an AI Memory Operating System and the memory layer for
persistent intelligence:

- [AI memory operating system philosophy](docs/ai-memory-operating-system-philosophy.md)
- [Memory lifecycle management direction](docs/memory-lifecycle-management-direction.md)
- [Context persistence as a first-class concern](docs/context-persistence-first-class-concern.md)

## Quick Start

> **Requirements.** Requires Python 3.11+ (developed and tested on 3.12). See
> [Supported Python versions & enforcement](docs/release-workflow.md#supported-python-versions--enforcement)
> for the full version policy.

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

# Turn docs and code structure into memory sources
mnemos ingest-docs ./docs
mnemos scan-code ./core

# Index durable markdown project-context sections
mnemos project-context capture ./project-context --project-id mnemos --json
mnemos project-context recall "architecture map" --project-id mnemos --trace-json

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

## Five practical demos

### 1. Capture, search, and read through the provider contract

Use this flow when a host, script, or agent needs stable machine-readable memory
operations instead of filesystem access:

```bash
mnemos capture --json --layer project --tag decision \
  --content "Use SQLite FTS5 as the first search stage; vector search is optional."

mnemos search --fast --json --limit 5 "SQLite FTS5 search stage"

mnemos read --json <item-id>
```

Expected result: each command returns provider-contract JSON. Search results
include stable fields such as `id`, `content`, `summary`, `layer`, `tags`,
`provenance`, `recency`, optional normalized `score`, and `metadata`; missing
items and degraded search paths return structured status/error fields rather
than forcing callers to parse human text.

### 2. Install host memory behavior for Claude Code and Cursor

Use this flow when a coding host should discover mnemos behavior from its own
configuration files:

```bash
mnemos install .
mnemos capabilities --json
mnemos context --render --host claude-code --prompt "What search contract did we choose?"
```

Expected result: `mnemos install` scaffolds the repo and, when host config
files are present, writes managed mnemos blocks for Claude Code and Cursor.
Claude Code has the strongest local adapter: a managed `CLAUDE.md` block plus
`settings.json` hooks for `PostToolUse`, `UserPromptSubmit`, and `Stop`.
The `PostToolUse` maintenance hook atomically elects one worker across concurrent
host sessions and launches GC, promotion, distillation, and duplicate detection
outside the synchronous tool-response path. Notable maintenance output is
spooled and delivered by a later tool event, so maintenance latency cannot block
normal tool use.
Cursor support is a managed rules block in `~/.cursor/rules` or
`~/.cursor/rules.md`; Cursor hook registration is not claimed because the local
adapter documents no equivalent hooks API. `mnemos context --render` emits a
bounded `<mnemos-context>` block that hosts can inject without reading storage
paths.

### 3. Treat docs and code structure as memory sources

Use this flow when a repo already has useful documentation or when an agent
needs a quick structural map of a codebase before a coding session:

```bash
mnemos ingest-docs ./docs
mnemos scan-code ./core
mnemos search "source docs architecture"
```

Expected result: `ingest-docs` captures supported document files as
source-backed project memories, and `scan-code` captures lightweight structural
summaries of code files, including detected symbols and imports. These commands
position document folders and code structure as inputs to mnemos' memory
lifecycle. They are not full replacements for dedicated wiki generators or
language-aware codegraph engines. See [Source Adapters](docs/source-adapters.md).

### 4. Recall durable project-context sections

Use this flow when a project keeps its canonical agent context in reviewed
markdown files such as `project-map.md`, `architecture.md`, `decisions.md`,
`workflows.md`, `domain-glossary.md`, and `open-risks.md`:

```bash
mnemos project-context capture ./project-context \
  --project-id mnemos \
  --tag agent-crew \
  --source-revision "$(git rev-parse HEAD)" \
  --json

mnemos project-context recall "How should backend agents handle search fallback?" \
  --project-id mnemos \
  --kind architecture \
  --active-file core/search.py \
  --agent-role backend \
  --trace-json

mnemos project-context audit ./project-context --project-id mnemos --json
```

Expected result: durable markdown remains the source of truth, while mnemos
indexes each heading section with stable IDs and source metadata. Recall JSON
returns `memory_id`, `score`, `content`, `source_path`, `source_section`,
`tags`, `updated_at`, and optional `source_revision`, plus a trace that
orchestrators can store with task audit logs. Audit JSON reports stale,
missing, and not-yet-indexed sections.

### 5. Produce operational evidence and inspect the store offline

Use this flow when you need evidence that the memory layer behaves consistently
over time, plus a human-readable view of stored memories:

```bash
mnemos beta-run --days 14 --seed 42 --output beta-report.json --json
mnemos inspect --output memory-inspect.html
mnemos graph --output domain-graph.html
```

Expected result: `beta-run` runs a deterministic simulated multi-day workflow
against an isolated real mnemos store; `inspect` writes a static offline HTML
inventory with search/filter/drill-down; `graph` writes a static offline HTML
domain relationship graph. No server or network is required for the generated
HTML files.

## Evidence-oriented comparison

The table below compares mnemos by local, repo-verifiable capabilities. It is
not a performance benchmark against external tools.

| Dimension | mnemos local evidence | What to verify |
|---|---|---|
| Local-first memory store | Markdown/YAML memory items under `wiki/` and `.agent/`, plus SQLite FTS runtime state | `mnemos install .`, [wiki repo structure](#wiki-repo-structure) |
| Stable host/provider contract | JSON commands for capture, search, context, read, GC, capabilities, and version | `mnemos capabilities --json`, [Stable Provider Contract](#stable-provider-contract) |
| Host adapters | Built-in `ClaudeCodeAdapter`, `CursorAdapter`, and `CodexAdapter`; host status includes Claude, Claude Code, Cursor, and Codex | `core/adapters/`, `core/provider.py` |
| Lifecycle policy | Policy-managed layers and stages from transient/session/project/global through archive/forget | [Memory Lifecycle](#memory-lifecycle) |
| Source adapters | `mnemos ingest-docs`, `mnemos scan-code`, and `mnemos project-context` turn documents, code structure, and durable markdown context sections into source-backed memory items | [Source Adapters](docs/source-adapters.md) |
| Deterministic evidence harness | `mnemos beta-run --days 14 --seed 42` sample: 46 captures, continuity recall `1.0`, relevance stability `1.0`, lifecycle violations `0`, degradation detected and recovered | [Beta Validation Harness](docs/beta-validation.md) |
| Offline inspection | Static `mnemos inspect` and `mnemos graph` HTML outputs; `mnemos ui` desktop view with optional extras | [Memory Inspection UI](docs/memory-inspection.md), [Domain-Relationship Graph View](docs/domain-graph-view.md), [Unified Inspection UI](docs/unified-inspection-ui.md) |
| Backup, restore, and sync | Portable backup/restore commands plus git-backed remote sync | [Backup & Restore](docs/backup-restore.md), [Remote Sync](docs/remote-sync.md) |

The documented beta-run numbers are a deterministic harness sample, not a
universal claim that mnemos is faster, more accurate, or production-grade for
every repository. Re-run the harness and inspect the JSON report for your own
acceptance criteria.

## How mnemos differs from llmwiki and codegraph-style tools

External projects change, so compare current versions directly before making a
tooling decision. Based on mnemos' local architecture, the difference is intent:

- llmwiki-like tools are usually centered on wiki or knowledge organization.
- codegraph-like tools are usually centered on code structure and relationship
  analysis.
- mnemos is centered on host-installable memory lifecycle: a CLI/provider JSON
  contract, policy-managed memory layers, host instructions/hooks where the host
  supports them, offline inspection/evidence surfaces, and backup/sync.
- `mnemos ingest-docs` and `mnemos scan-code` intentionally treat docs and code
  structure as memory sources. The product strategy is to remember and retrieve
  those inputs inside agent workflows, not to compete head-on as a prettier
  public wiki renderer or a full language-server codegraph.
- `mnemos project-context` intentionally keeps markdown files canonical and
  uses mnemos as the retrieval/index layer for section-level agent context.

This README does not claim mnemos is a superset or replacement for those
categories. It claims mnemos owns a different boundary: durable operational
memory for AI coding agents.

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
| `core/vector.py` | Optional vector search backend (Qdrant / Chroma / QMD; graceful fallback) |
| `core/qmd.py` | Bounded argv/JSON adapter for repo-local QMD retrieval and index refresh |
| `core/qmd_queue.py` | Durable coalescing worker queue for derived QMD index consistency |
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
| `agents/source_adapters.py` | Source adapters — turn document folders and code files into source-backed memory candidates |
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
| `mnemos search` | Search across memory layers without changing memory state |
| `mnemos search --touch` | Deprecated legacy search mode that increments `access_count` |
| `mnemos recall --json --request-file FILE` | Read-only structured Recall provider contract |
| `mnemos feedback --json --request-file FILE` | Record applied/validated memory usage events |
| `mnemos read` | Read a specific item by ID |
| `mnemos use` | Mark an item as "in use" |
| `mnemos edit` | Update item content |
| `mnemos promote` | Promote to next (or specified) layer |
| `mnemos demote` | Demote to a lower layer |
| `mnemos archive` | Soft-delete (retain content) |
| `mnemos forget` | Hard-delete (requires archived state; use `--force` to skip prompt) |
| `mnemos qmd-prepare --json` | Prepare repo-local QMD config without invoking QMD |
| `mnemos qmd-index-worker --status --json` | Inspect the QMD refresh queue without consuming it |
| `mnemos qmd-index-worker --retry-failed --json` | Requeue failed QMD refresh jobs and signal a worker |
| `mnemos qmd-evaluate --fixture FILE --json` | Evaluate labelled offline retrieval rankings |
| `mnemos log` | Manually append or view audit log entries |
| `mnemos capabilities --json` | Print stable machine-readable provider features |
| `mnemos version --json` | Print version and compatibility metadata |
| `mnemos ingest-claude-md` | Discover and ingest CLAUDE.md files into memory |
| `mnemos ingest-docs SOURCE_DIR` | Ingest supported document files as source-backed memories |
| `mnemos scan-code SOURCE_DIR` | Capture lightweight code-structure memories from source files |
| `mnemos project-context capture SOURCE` | Capture or update markdown project-context sections with stable source metadata |
| `mnemos project-context recall QUERY` | Return structured project-scoped recall results and trace JSON |
| `mnemos project-context audit SOURCE` | Report stale, missing, and not-yet-indexed project-context section captures |

## Stable Provider Contract

Host integrations should use the CLI/provider contract instead of reading
mnemos storage paths, SQLite FTS tables, or Markdown filenames directly.

```bash
mnemos capture --json --content "Architecture decision..." --layer project
mnemos search --fast --json --limit 5 "architecture decision"
mnemos project-context recall "architecture decision" --project-id my-project --trace-json
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

`mnemos project-context recall --trace-json` is the stable structured recall
surface for agent-workflow project context. Each result includes `memory_id`,
`score`, `content`, `source_path`, `source_section`, `tags`, `updated_at`, and
optional `source_revision`. The trace lists the memory IDs and source sections
used so an orchestrator can cite the original markdown and reload it when
needed.

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
- Codex: `~/.codex/AGENTS.md` when present.

The managed blocks describe capture, search, read, and GC behavior independently
of agent-crew. When a host lacks hook support or expected config files, install
skips that host-specific surface without failing the repo scaffold.

Host capability status is exposed by `mnemos capabilities --json`:

| Host | Locally supported surface |
|---|---|
| Claude / Claude Code | Autonomous capture, context injection, and daemon runtime are marked `supported`; the built-in Claude Code adapter manages `CLAUDE.md` and `settings.json` hooks when safe host files exist. |
| Cursor | Daemon runtime is marked `supported`; autonomous capture is `unsupported`; context injection is `unknown`; the built-in Cursor adapter manages rules files only. |
| Codex | Daemon runtime is marked `supported`; autonomous capture is `unsupported`; context injection is `unknown`; the built-in Codex adapter manages `AGENTS.md` behavior instructions but does not claim hook-based context injection. |

MCP-style integrations should treat mnemos as a stable subprocess/provider
contract: call `mnemos capture/search/context/read/... --json`, inspect
`mnemos capabilities --json`, and avoid direct reads from `.agent/`, `wiki/`,
or SQLite internals. This repository does not claim to ship an MCP server.

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
- QMD is configured under `retrieval.qmd` in `mnemos.yml`; it is disabled by
  default and keeps config/cache state repo-local under `.agent/state/qmd/`.

Install with vector extras:

```bash
pip install -e ".[vector]"
```

QMD remains an optional external executable and is never installed or allowed
to download models implicitly by Mnemos. See the
[QMD integration and recovery guide](docs/qmd-integration.md) for model-free
setup, explicit Korean/Qwen3 readiness, queue diagnostics, and offline
evaluation.

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
