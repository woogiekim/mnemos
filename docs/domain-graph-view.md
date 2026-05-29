# Domain-Relationship Graph View

> Public design doc for issue #68 — the graph-view UI built on top of the
> issue-#67 domain-relationship model in `core/cohesion.py`.

> See also: [Memory Inspection UI](./memory-inspection.md) — the sibling
> `mnemos inspect` surface (issue #80) that follows the same static-HTML
> packaging pattern but surfaces per-memory inventory (trust, provenance,
> lifecycle) rather than cross-memory cohesion.

> See also: [Unified Inspection UI](./unified-inspection-ui.md) — the `mnemos ui`
> desktop app (issue #83) that combines this graph with the raw-memory inspect
> surface and a policy-cohesion panel; it reuses `build_graph_payload` verbatim
> for its Graph tab.

## Background

Issue #67 introduced `core/cohesion.py`, a read-only derivation layer that
turns the stored memory items into a higher-level view: **Domains** (cohesive
clusters keyed by tag namespace), **inter-memory relationships** (`shares_tag`,
`same_layer`, `co_domain`, `promotion_lineage`, `references`), and a top-level
`DomainGraph` wrapper. The model is closed and versioned via
`SCHEMA_VERSION = 1`.

Until now this graph existed only as Python dataclasses — invisible to the
human operator. Issue #68 fills that gap. From the issue:

> nodes = domains, edges = relationships; click node → drill down to that
> domain's member memories.

This document records the design DECISION for how the graph is surfaced and
why the alternatives were rejected.

## Goal — Acceptance Criteria

1. A design doc for the graph view (this document).
2. A data contract consuming the issue-#67 relationship model (no duplication).
3. A documented DECISION on how to add a UI surface to a CLI-only codebase,
   with surface tradeoffs.
4. Node → memory drill-down interaction defined and implemented.

## DECISION — dependency-light static HTML export

A new Click subcommand:

```text
mnemos graph [--output PATH] [--layer LAYER ...] [--limit N]
             [--preview-width N] [--full] [--open/--no-open]
```

builds the `DomainGraph`, augments it with a `memories` map for drill-down,
and writes a **single self-contained HTML file** that the user opens in any
modern browser. The HTML embeds the JSON payload inline and ships a small
hand-written canvas force-directed renderer in the same file.

- **No server.** No long-running process, no port, no auth surface.
- **No framework.** No React/Vue/Svelte, no npm, no build step.
- **No network.** No CDN, no fonts, no analytics. The file works offline.
- **One artifact.** The user can `scp` it, email it, or archive it. The HTML
  is self-sufficient.

## Rejected Alternatives

| Alternative | Why rejected |
|---|---|
| **Local web server** (`mnemos serve`, Flask/FastAPI) | Adds a long-running process, a server framework dependency (~8–15 MB transitive closure), and an open port to a CLI-only tool. Two windows to keep alive (server + browser). The drill-down can be done entirely client-side; the server adds latency for no gain. |
| **External graph CLI tool** (Graphviz / `dot`, mermaid-cli, vis-network) | Forces a non-Python dependency onto every operator (Graphviz binary, Node runtime). Loses interactivity: static `.svg` cannot drive node-click drill-down, which is acceptance criterion #4. |
| **JS framework + bundler** (React+Vite, Vue+Rollup, Svelte+esbuild) | Pulls in npm, `package.json`, and 50–200 MB of `node_modules` to render N≤100 circles and lines. The 100%-coverage gate is Python-only; a JS bundle would need its own ad-hoc verification regime. |
| **Electron / Tauri desktop app** | Wildly disproportionate. Adds 100+ MB of runtime, OS-specific packaging, and signing/notarization workflows. None of acceptance criteria #1–4 need a native shell. |

The chosen approach (vendored ~200-LOC JS in one HTML file) preserves the
CLI-only architecture, has zero runtime dependencies beyond a browser, and
ships end-to-end interactivity in a single artifact.

## Data Contract

`mnemos graph` consumes — never duplicates — the issue-#67 model. The wire
format embedded in the HTML is exactly:

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-29T05:30:00+00:00",
  "domains":       [ /* DomainGraph.to_dict().domains, pass-through */ ],
  "relationships": [ /* DomainGraph.to_dict().relationships, pass-through */ ],
  "memories": {
    "<memory_id>": {
      "id":              "<memory_id>",
      "layer":           "<layer>",
      "tags":            ["..."],
      "content_preview": "<truncated to --preview-width, or full when --full>"
    }
  }
}
```

`schema_version`, `generated_at`, `domains[]`, `relationships[]` are
**unchanged** from `DomainGraph.to_dict()`. The only addition is `memories`,
the drill-down lookup keyed by every id that appears in any
`domain.member_ids`. The `memories` map is issue-#68-specific; it is not part
of the issue-#67 model.

The payload is `JSON.parse`'d once on load. There is no second network round
trip.

### Schema-version guard

`build_graph_payload` asserts `schema_version == 1` and raises `ValueError`
on mismatch. If `core/cohesion.py` ever bumps `SCHEMA_VERSION`, this command
fails loudly until the implementer reconciles the change.

## Drill-Down UX

1. **Initial load** — the canvas renders nodes (circles, radius scaled by
   `cohesion_score`) and edges (color and dash style scaled by relationship
   `kind`). The right-hand drill-down panel is hidden.
2. **Click a node** — the panel opens, showing the domain label, cohesion
   score, and a list of member memories. Each entry shows the memory id, the
   layer it lives in, its tags, and a content preview.
3. **Close the panel** — the × button in the panel closes it. The **Escape**
   key also closes the panel from anywhere on the page.
4. **Empty state** — when no domains exist (no memories captured yet), the
   page footer shows: `no domains yet — capture some memories first`.
5. **Schema-version footer** — every render shows `schema_version=1 ·
   generated_at=<iso>` so the user can see which contract version produced the
   page.

The drill-down panel is `role="complementary"` and keyboard-dismissible. Mouse
click is the primary node-selection input; keyboard node navigation is
non-mandatory in MVP (the canvas approach makes per-node tab focus
non-trivial; mouse + ESC dismiss is the minimum).

## Vendored Renderer Constraints

The single inline `<script>` block satisfies:

- **No external dependencies.** No `<script src=…>`, no `import`, no `require`.
- **No network at runtime.** No CDN, no font URL, no analytics.
- **No unsafe eval.** No `eval`, no `new Function(...)`, no string-body `setTimeout`.
- **Size budget.** Target ~150–200 LOC of JS, hard cap ~250 LOC. The renderer
  is meant to be read in one sitting.
- **Renderer technique.** Canvas-based hand-written force-directed layout
  (repulsion + spring + center bias). Forces run for a fixed number of frames
  on initial layout; redraw is event-driven thereafter.
- **Visual encoding.** Node radius scales with `cohesion_score`. Edge color
  and dash style vary by relationship `kind` — solid blue for `shares_tag`,
  dashed gray for `same_layer`, solid green for `co_domain`, solid orange for
  `promotion_lineage`, dashed orange for `references`.

## Non-Functional Requirements

- **No network.** Emitted HTML works fully offline.
- **Additive only.** `core/cohesion.py` is consumed, not modified. Only one
  existing file is edited: `core/cli.py` (one new `@cli.command("graph")`).
  No existing CLI command behavior changes.
- **No new dependencies.** `pyproject.toml` is unchanged. Standard library
  only (`json`, `webbrowser`, `pathlib`, `importlib.resources`).
- **Coverage gate.** The new `core/graphview.py` is covered by tests to 100%.
  `core/templates/graph.html` (non-Python) is automatically out of scope
  because `[tool.coverage.run] source = ["core", "agents"]` only counts
  `.py` files in packages with `__init__.py`; the templates directory has
  none.
- **Warnings-as-errors.** Tests run under `filterwarnings = ["error"]`. The
  default `--no-open` ensures `webbrowser.open` is never invoked by tests, so
  no `ResourceWarning` is raised.
- **KISS / YAGNI / DRY.** Three public Python functions, one HTML template,
  one new CLI subcommand. No premature abstractions.
- **Idempotent.** Running `mnemos graph` twice overwrites the same output
  file.

## How to Use

Generate the graph file (defaults to `./domain-graph.html`):

```bash
mnemos graph
```

Open the file in your browser yourself:

```bash
open ./domain-graph.html      # macOS
xdg-open ./domain-graph.html  # Linux
```

Or let mnemos open it for you:

```bash
mnemos graph --open
```

Restrict to a single layer:

```bash
mnemos graph --layer session
```

Cap the number of source memories (smaller graph, faster render):

```bash
mnemos graph --limit 100
```

Show full memory content in the drill-down panel (the default truncates to
240 characters):

```bash
mnemos graph --full
```

Custom preview width:

```bash
mnemos graph --preview-width 80
```

Custom output path:

```bash
mnemos graph --output /tmp/snapshot.html
```

## See Also

- `core/cohesion.py` — issue #67, the relationship model this view consumes.
- `docs/domain-policy-cohesion.md` — the cohesion derivation rationale.
- Issue #68 — the original UI request.
