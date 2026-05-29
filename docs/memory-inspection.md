# Memory Inspection UI

> Operator guide for issue #80 — the static-HTML memory inspection surface
> shipped as `mnemos inspect`. Parallels `mnemos graph` (issue #68) but
> focused on **per-memory inventory** rather than cross-memory cohesion.

> Scope: surface mnemos's *already-persisted* metadata in a single offline
> HTML file. This document does not propose any new schema. Where the issue
> #80 acceptance criteria mention "trust level" or "provenance" — concepts
> mnemos does not store as first-class enums — the UI surfaces an *honest
> mapping* of existing fields and the docs flag the gap. See § Honest
> mappings below.

> Companion docs — cross-linked, not duplicated:
>
> - [Unified Inspection UI](./unified-inspection-ui.md) — the `mnemos ui`
>   desktop app (issue #83) that combines this inspect surface with the domain
>   graph and a policy-cohesion panel; it reuses `build_inspect_payload`
>   verbatim for its Memory tab.
> - [Domain-Relationship Graph View](./domain-graph-view.md) — sibling
>   `mnemos graph` UI, same packaging/template pattern.
> - [Security & Privacy](./security-privacy.md) — what stays local; the
>   inspect HTML is local-only by construction (no network calls in the
>   rendered file).
> - [Backup & Restore](./backup-restore.md) — the inspect view reads only
>   what the backup snapshots ship; restore is sufficient to reproduce the
>   inspect HTML.

## TL;DR

```bash
mnemos inspect --output ./memory-inspect.html
# or to embed full memory bodies instead of 240-char previews:
mnemos inspect --output ./memory-inspect.html --full
# or to scope to a layer:
mnemos inspect --output ./memory-inspect.html --layer global --layer session
# open immediately in the default browser:
mnemos inspect --output ./memory-inspect.html --open
```

The command writes a single self-contained `.html` file. Open it directly
(`file://…/memory-inspect.html`); no server, no network, no JS bundler.

## CLI surface

```text
mnemos inspect [--output PATH] [--layer LAYER ...] [--limit N]
               [--preview-width N] [--full] [--open/--no-open]
```

| Flag | Default | Meaning |
|---|---|---|
| `--output` | `./memory-inspect.html` | Destination file. Parent dirs are created. |
| `--layer` | (all) | Repeatable. Restrict source items to the named layer(s). |
| `--limit` | (none) | Cap the number of source memory items. |
| `--preview-width` | `240` | Character cap for the drill-down content preview. |
| `--full` | off | Embed full memory content (overrides `--preview-width`). |
| `--open` / `--no-open` | `--no-open` | Open the rendered HTML in the default browser. |

The flag surface intentionally mirrors `mnemos graph` so operators
familiar with #68 need no relearning.

## What the page shows

The page is a three-column layout:

- **Left sidebar** — search input (`role="search"`, debounced 100ms; matches
  content + tags + id), layer-filter checkboxes, stage-filter checkboxes,
  "show archived only" toggle, "show promotable only" toggle.
- **Center list** — one row per memory with id, layer/stage badges,
  archived/promotable badges when applicable, tags, and a one-line content
  preview. An `aria-live="polite"` count above the list announces how many
  match.
- **Right drill-down** — opens when a row is clicked. Contains four panels:
  Content, Trust, Provenance, and Lifecycle. Each panel has a
  `data-panel="<name>"` anchor that the AC-anchor tests pin.

## Acceptance criteria → UI controls

| AC | UI control | Test anchor |
|---|---|---|
| 1. Memory search UI | `<input role="search">` in the sidebar; client-side filter across `content`, `tags`, `id`. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac1_search_input_present` |
| 2. Memory content viewer | Right-side drill-down with `<pre class="content">` (`id="dd-content"`) inside `data-panel="content"`; opens on row click. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac2_content_viewer_scaffold_present` (`id="drilldown"`) + `tests/test_inspectview.py::TestTruncation` for the `--full` viewer payload. |
| 3. Lifecycle / layer filtering | `<div id="layer-filters">` and `<div id="stage-filters">` checkbox lists in the sidebar, driven by `payload.layers` / `payload.stages` enums. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac3_layer_and_stage_filter_controls` + `tests/test_inspectview.py::TestPayloadShape::test_layers_array_is_canonical_chain` and `::test_stages_array_matches_policy`. |
| 4. Trust level visible | `data-panel="trust"` panel surfacing the `(layer, quality_score, access_count)` triple. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac4_trust_panel_present` + `tests/test_inspectview.py::TestItemProjection::test_full_field_set_present`. |
| 5. Source / provenance inspectable | `data-panel="provenance"` panel surfacing `run_id`, `session_id`, `created_at`, `content_hash`, `path`. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac5_provenance_panel_present` + `tests/test_inspectview.py::TestItemProjection::test_full_field_set_present`. |
| 6. Promotion / archive state visible | `data-panel="lifecycle"` panel surfacing `stage`, `promotable`, `next_layer`, `archived`; precomputed via `PolicyEngine.check_promotion_eligible`. | `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_ac6_lifecycle_panel_present` + `tests/test_cli_inspect.py::TestInspectSixAcAnchors::test_payload_surfaces_promotable_and_next_layer` + `tests/test_inspectview.py::TestPromotionPrecomputation`. |

## How it differs from `mnemos graph`

| Surface | What it answers | What it shows |
|---|---|---|
| `mnemos graph` (#68) | "What is the high-level shape of the memory store?" | Domains as nodes, inter-memory relationships as edges; drill-down to a *cluster's* member memories. |
| `mnemos inspect` (#80) | "What does this specific memory record actually carry?" | One row per memory; drill-down to a *single memory's* trust, provenance, and lifecycle metadata. |

The two are complementary. The graph view answers "are my memories
coalescing into the domains I expect?"; the inspect view answers "for
this specific memory — where did it come from, can it be trusted, can it
be promoted, has it been archived?"

## Honest mappings

Issue #80 lists "trust level", "source/provenance", and "promotion/archive
state" as acceptance criteria. mnemos does *not* currently store any of
these as first-class enums. Rather than invent a schema for #80, the
inspect view surfaces the closest *existing* fields and labels them
honestly. If any of these gaps matter for a downstream consumer, file a
follow-up RFC to land a real schema.

### Trust level

There is no `trust_level` field in mnemos's persisted memory metadata.
The inspect view surfaces a **(layer, quality_score, access_count)
triple** in the Trust panel as a *proxy*:

- `layer` is the canonical position in the promotion chain
  (`transient → ephemeral → working → session → project → global`);
  higher layers have survived more lifecycle gates.
- `quality_score` is the `[0.0, 1.0]` capture-time quality assignment
  (`core/gateway.py`).
- `access_count` is the live counter incremented on every read/search
  hit (`core/gateway.py:627-639`).

These three together are the best available proxy for "how much should
I trust this memory?" — and the values map directly onto the existing
promotion thresholds in `repo/wiki/policy.yaml`. If you want a real
single-enum `trust_level` (e.g. `low/medium/high`), open an RFC. The
inspect view is forward-compatible: a future `trust_level` field would
just become a new dl row in the Trust panel without disturbing the
existing three.

### Provenance / source

mnemos does not currently store a per-item "adapter source" (`obsidian`,
`mcp`, `manual`, `hook`, …) as a first-class field — captures are
attributed to whichever gateway instance happened to write them. The
inspect view *reconstructs* provenance from five existing fields:

- `run_id` — the calling agent run that produced the capture
  (`core/gateway.py:158`).
- `session_id` — the calling session
  (`core/gateway.py:159`).
- `created_at` — UTC ISO-8601 capture timestamp
  (`core/store.py:281-285`).
- `content_hash` — SHA-256 of NFKC-normalised content
  (`core/gateway.py:516`); useful for deduplication audits.
- `path` (`_path` internally) — concrete filesystem path under
  `repo/wiki/…` or `.agent/…`.

Together these tell you *who* captured the memory, *when*, *what was
captured* (via hash), and *where the file lives*. If a per-item
`adapter_source` field is needed (e.g. to filter "only obsidian
captures"), file a follow-up RFC; the field would slot into the
Provenance panel exactly like `run_id` does today.

### Promotion / archive state

This one is mostly faithful — mnemos *does* store `stage`
(`core/policy.py:15-29 VALID_STAGES`) as a first-class enum.
The inspect view's Lifecycle panel surfaces:

- `stage` directly from the YAML front-matter.
- `archived` as the simple boolean `stage == "archived"`.
- `promotable` and `next_layer` **precomputed** at payload-build time
  by calling `core.policy.PolicyEngine.check_promotion_eligible(item)`
  and `PolicyEngine.get_next_layer(item.layer)`. The UI never re-derives
  thresholds — that would risk drift from `repo/wiki/policy.yaml`.

## Packaging

The HTML template ships as wheel package-data via the existing
`pyproject.toml` entry:

```toml
[tool.setuptools.package-data]
core = ["templates/*.html"]
```

Loaded at runtime via the warning-free `importlib.resources` form:

```python
from importlib.resources import files
files("core.templates").joinpath("inspect.html").read_text("utf-8")
```

Regression test: `tests/test_inspect_packaging.py` mirrors
`tests/test_graph_packaging.py` — builds a wheel, installs it into a
fresh venv, asserts the template is shipped and reachable, and runs
`mnemos inspect` end-to-end inside the installed venv.

## Out of scope

- A first-class `trust_level` enum, a first-class `adapter_source` field,
  a per-item audit timeline view, or any other new schema. Flagged here
  rather than added.
- Vector / embedding inspection — out of scope for #80; the rendered HTML
  does not surface embeddings.
- Web server / live dashboard — static HTML only, by issue-#68 precedent.
- Network calls from the rendered HTML — explicitly forbidden by the
  template safety test (`test_no_fetch_xhr_or_eval_in_rendered_html`).
