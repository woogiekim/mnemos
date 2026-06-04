# Source Adapters

mnemos can ingest existing documents and lightweight code structure as memory
sources for AI coding agents. The goal is not to replace a wiki renderer or a
language-aware codegraph engine. The goal is to make useful project context
durable, searchable, policy-managed, and available through the same memory
contract as captured decisions.

## Positioning

Use these adapters when you want:

- a document folder to become searchable agent memory
- a quick structural map of important code files before or during a coding task
- source-backed memory items that can be searched, read, inspected, backed up,
  synced, and distilled through normal mnemos workflows

Do not treat these adapters as:

- a full static-site or public wiki generator
- a complete call graph, type graph, or language-server index
- a replacement for dedicated documentation or code navigation tools

## Commands

```bash
mnemos ingest-docs ./docs
mnemos scan-code ./core
mnemos project-context capture ./project-context --project-id mnemos --json
```

Both commands default to the `project` layer and recursively scan the supplied
directory. They support:

```bash
mnemos ingest-docs ./docs --layer global --run-id docs-import
mnemos ingest-docs ./docs --dry-run
mnemos ingest-docs ./docs --no-recursive --limit 10

mnemos scan-code ./core --layer project --run-id code-map
mnemos scan-code ./core --dry-run
mnemos scan-code ./core --no-recursive --limit 25

mnemos project-context capture ./project-context --project-id mnemos --tag agent-crew --json
mnemos project-context recall "architecture fallback" --project-id mnemos --kind architecture --trace-json
mnemos project-context audit ./project-context --project-id mnemos --json
```

## Document Adapter

`mnemos ingest-docs` reads supported text document files and captures each file
as a source-backed memory item.

Supported suffixes:

- `.md`
- `.markdown`
- `.txt`
- `.rst`
- `.adoc`
- `.html`
- `.htm`

Each captured memory includes:

- source type: `docs_folder`
- source scope: `docs_folder`
- absolute `source_file`
- relative path metadata
- tags such as `source:docs`, `docs`, `ext:md`, and path-derived tags

## Code Adapter

`mnemos scan-code` reads supported code files and captures a compact structural
summary for each file. Python files use `ast` for class, function, and import
detection. Other supported languages use lightweight regex extraction for common
symbols and imports.

Each captured memory includes:

- source type: `code_scan`
- source scope: `codebase`
- absolute `source_file`
- relative path metadata
- detected language and line count
- tags such as `source:code`, `code`, `lang:python`, `ext:py`, and path-derived tags

The code adapter intentionally stores a concise memory summary, not full source
code. This keeps retrieved context small enough for agent workflows while still
making code structure searchable.

## Deduplication

Both adapters deduplicate by `source_file` within the target layer:

- unchanged source content is skipped
- changed source content updates the existing memory item in place
- new source files create new memory items

This preserves stable memory IDs for source-backed context while still allowing
the underlying source files to evolve.

## Durable Project-Context Recall

`mnemos project-context` is for project context packs that remain canonical as
markdown files. Typical files are:

- `project-map.md`
- `architecture.md`
- `decisions.md`
- `workflows.md`
- `domain-glossary.md`
- `open-risks.md`

Mnemos indexes heading sections from those files. It does not replace the
markdown source of truth. Agents can recall indexed sections quickly and then
use `source_path` plus `source_section` to cite or reload the original markdown.

### Capture Or Update Sections

```bash
mnemos project-context capture ./project-context \
  --project-id mnemos \
  --project-root . \
  --source-revision "$(git rev-parse HEAD)" \
  --tag agent-crew \
  --json
```

The stable memory ID is derived from project identity, project root hash,
relative source path, and heading. Re-running capture after editing the same
heading updates the existing memory instead of creating a new ID.

Each stored section includes metadata and tags for:

- `project_id`
- `project_root` and `project_root_hash`
- `kind`
- `source_path`
- `source_file`
- `source_section`
- `source_content_hash`
- optional `source_revision`
- tags such as `project:<id>`, `project_root:<hash>`, `kind:<kind>`,
  `source_path:<path>`, and `source_section:<heading>`

Kinds are inferred from common context-pack filenames when possible:
`architecture.md` becomes `architecture`, `decisions.md` becomes `decision`,
`workflows.md` becomes `workflow`, `open-risks.md` becomes `risk`,
`domain-glossary.md` becomes `glossary`, and `project-map.md` becomes
`project-map`.

### Recall With Structured Trace

```bash
mnemos project-context recall "search fallback contract" \
  --project-id mnemos \
  --kind architecture \
  --active-file core/search.py \
  --agent-role backend \
  --context-tag durable \
  --trace-json
```

Recall output is JSON designed for orchestrators. Each result includes:

- `memory_id`
- `score`
- `content`
- `source_path`
- `source_section`
- `tags`
- `updated_at`
- optional `source_revision`

The trace includes the original query, the enriched query hints, filters, and
the memory IDs/source sections used. Query enrichment records project identity,
active files, agent role, and context tags without requiring agents to read
storage internals.

### Freshness Audit

```bash
mnemos project-context audit ./project-context --project-id mnemos --json
```

Audit compares current markdown sections with indexed memories and reports:

- `fresh`: indexed content matches current markdown metadata
- `stale`: section exists but content hash, path, heading, or revision changed
- `missing`: indexed memory points to a section no longer found in the source
- `not_indexed`: source section exists but has not been captured yet

Capture, recall, and audit return parseable degraded JSON for backend failures
instead of blocking agent workflows with unstructured errors.

## Ignored Directories

Recursive scans skip common generated and private directories:

- `.agent`
- `.git`
- `.hg`
- `.svn`
- `.venv`
- `venv`
- `__pycache__`
- `node_modules`
- `dist`
- `build`
- `.mypy_cache`
- `.pytest_cache`

Files larger than 512 KB are skipped to keep ingestion predictable.
