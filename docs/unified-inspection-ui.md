# Unified Inspection UI

> Operator guide for [issue #83](https://github.com/woogiekim/mnemos/issues/83)
> — the single static-HTML surface that unifies the three existing read-only
> views (domain graph, raw-memory inspect, policy cohesion) and ships as a
> native **desktop app** via `mnemos ui`.

> Scope: presentation only. The unified UI reuses the *existing* payload
> builders and never changes the cohesion / policy / gateway / store APIs or
> any on-disk format. Persisting derived domains/policies is the separate
> [issue #84](https://github.com/woogiekim/mnemos/issues/84).

> Companion docs — cross-linked, not duplicated:
>
> - [Domain-Relationship Graph View](./domain-graph-view.md) — the `mnemos graph`
>   surface (issue #68); its `build_graph_payload` is reused verbatim for the
>   Graph tab here.
> - [Memory Inspection UI](./memory-inspection.md) — the `mnemos inspect` surface
>   (issue #80); its `build_inspect_payload` is reused verbatim for the Memory tab.

## TL;DR

```bash
pip install 'mnemos[ui]'   # one-time: installs the optional pywebview extra
mnemos ui                  # launches the unified UI in a native desktop window
```

No GUI / running headless or in CI? Write the same self-contained HTML to a file
instead — this path never touches pywebview:

```bash
mnemos ui --output ./mnemos-ui.html
```

## What it combines

`mnemos ui` renders **one** self-contained HTML surface with three tabs:

| Tab | Source | What you see |
|---|---|---|
| **Graph** | `core.graphview.build_graph_payload` (#68) | A canvas force-directed domain graph. Click a domain node to cross-filter the Memory tab to that domain's member memories. |
| **Memory** | `core.inspectview.build_inspect_payload` (#80) | Search + drill-down with **trust** (layer / quality_score / access_count), **provenance** (run_id / session_id / created_at / content_hash / path), and **lifecycle** (stage / promotable / next_layer / archived) panels. |
| **Policy Cohesion** | `core.cohesion.aggregate_policy_cohesion` (#67) | A table of recurring `constraint:` / `decision:` / `preference:` themes (theme / members / layers / recurrence / suggested_layer). Until now this aggregation had no CLI/UI surface — the unified UI is its first home. Click a cluster to cross-filter the Memory tab to its member ids. |

All three sections are built by **reusing** the existing builders — the unified
view never reimplements the graph or memory payloads.

## Desktop app (default) — pywebview

`mnemos ui` with no `--output` launches the unified HTML inside a **native OS
webview window** (pywebview). HTML remains the rendering technology; pywebview
just hosts the rendered string in a desktop window — no web server, no browser
tab, no network call.

pywebview is an **optional** dependency, declared as the `[ui]` pip extra so the
base mnemos install stays dependency-light:

```bash
pip install 'mnemos[ui]'   # pulls pywebview>=5.0
```

The base package never imports pywebview. Only the window-launch shim
(`core.unifiedview.launch_app`) touches it, and that import is lazy — every other
unified-view function (payload build, HTML render, file write) works with the
base install.

### When the `[ui]` extra is missing

If you run `mnemos ui` (desktop mode) without the extra installed, the command
fails **loudly** with an actionable hint and a non-zero exit code — it does not
silently fall back to writing a file:

```
$ mnemos ui
pywebview is not installed. Install the UI extra with: pip install 'mnemos[ui]'
  — or render to a file with: mnemos ui --output ./mnemos-ui.html
$ echo $?
1
```

This keeps headless misuse obvious rather than surprising. The explicit headless
path is always `--output`.

## Headless / CI fallback — `--output`

```bash
mnemos ui --output ./mnemos-ui.html
```

Writes the same self-contained HTML file and prints `[mnemos] wrote <path>`. This
path **never imports pywebview**, so it works on a server, in CI, or anywhere
without a display — no `[ui]` extra required. Open the file later in any browser
or feed it to an artifact store.

## Controllable edge density

The real store is a **49-node / ~33,671-edge hairball** (32,850 `shares_tag` +
631 `co_domain` + 190 `same_layer` edges). Rendered raw, the Graph tab is an
unreadable mess. Two knobs tame it; the Graph tab shows an honest banner
("Showing top N of M edges …") so you always know what was dropped:

| Flag | Default | Effect |
|---|---|---|
| `--max-edges-per-node N` | `8` | Keep at most `N` incident edges per node, greedily preferring the heaviest. With the default, ≤392 of 33,671 edges survive — a readable graph out of the box. `0` (or negative) disables the cap. |
| `--edge-weight-threshold W` | `0.0` | Drop every edge whose weight is below `W` **before** the per-node cap. `0.0` keeps all edges; raise it to prune weak `shares_tag` links. |

```bash
# Tighter graph: at most 4 edges/node, drop weak (<0.3) links first.
mnemos ui --output ./ui.html --max-edges-per-node 4 --edge-weight-threshold 0.3
```

The capping operates on a **copy** of the graph payload — it never mutates the
underlying `#68` graph output.

## Cross-filtering (pure client-side)

Cross-navigation is entirely client-side over the embedded payload (no server,
no extra requests):

- **Graph → Memory**: click a domain node → the UI switches to the Memory tab,
  filtered to that domain's `member_ids`.
- **Policy → Memory**: click a policy-cohesion cluster → the Memory tab is
  filtered to that cluster's `member_ids`.
- A **clear** affordance removes the active filter and returns the full list.

The cross-filter is sound because the graph member-id space is a subset of the
memory id space (`graph.domains[].member_ids ⊆ memory.memories[].id`).

## Other flags

| Flag | Default | Effect |
|---|---|---|
| `--layer L` (repeatable) | all layers | Restrict the source items to the named layer(s) — same raw single-store walk as `mnemos inspect`. |
| `--limit N` | unlimited | Cap the number of source memory items. |
| `--preview-width N` | `240` | Character cap for drill-down content previews. |
| `--full` | off | Embed full memory content verbatim (overrides `--preview-width`). |

## Safety guarantee

The rendered HTML is **self-contained and offline by construction**: no network
call, no `fetch` / `XMLHttpRequest`, no `eval` / `new Function`, no CDN. The
payload is embedded as a `<script type="application/json">` block and parsed
client-side, so the same file works identically inside the pywebview window and
when opened directly from disk (no `file://` CORS surprises).

## Domain sidebar (Memory tab)

Operator guide for [issue #86](https://github.com/woogiekim/mnemos/issues/86)
— a left-hand sidebar inside the Memory tab that lists every domain plus a
pinned "All memories" row.

The sidebar is a sibling of the existing list + drill-down columns, so the
three columns coexist inside the Memory tab on a 1280×860 window:

| Column | Width budget |
|---|---|
| `#domain-sidebar` | fixed `220px` (~17.2%) |
| `#memory-list-col` | flex `1 1 60%` (~49.7%) |
| `#drilldown` | flex `1 1 40%` (~33.1%) |

All three columns set `min-width: 0` so the row stays scrollable inside the
absolute-positioned `.tab` box (the [#83](https://github.com/woogiekim/mnemos/issues/83)
layout chain — `body{height:100vh}` + `main{height:0}` + `.tab{position:absolute; inset:0}`
— is preserved verbatim and untouched by this change).

Row shape:

| Row | Source | Effect |
|---|---|---|
| Pinned "All memories" (`data-domain-row="__all__"`) | always present | clicking clears the active cross-filter via the existing `clearFilter()` |
| One row per `d` in `graph.domains` | alphabetized by `d.label.toLowerCase()` | clicking calls the existing `applyCrossFilter(d.member_ids, "domain …")` — the SAME function the [Graph node click](#cross-filtering-pure-client-side) and the [Policy cluster click](#cross-filtering-pure-client-side) already funnel through |

Each row is a real `<button>` with `role="listitem"` and `tabindex="0"`. The
sidebar honors keyboard navigation:

| Key | Effect |
|---|---|
| `Tab` / `Shift+Tab` | default browser order — across the nav, the sidebar, the search input, and the result list |
| `ArrowUp` / `ArrowDown` | move focus across sidebar rows |
| `Enter` / `Space` | activate the focused row |

Selection (`aria-selected="true"`) is a **visual hint** only — the
authoritative filter state remains `activeFilterIds`, the same state the
existing graph + policy cross-filter sets. The sidebar therefore does NOT
change the search match function, the row rendering, or any persisted
field; it is pure presentation.

The sidebar is implemented entirely inside the existing IIFE in
`core/templates/ui.html` — no Python change, no payload change, no schema
bump.

## Interactive graph (Graph tab)

Operator guide for [issue #86](https://github.com/woogiekim/mnemos/issues/86)
— an Obsidian-style pointer state machine on the Graph canvas. Replaces the
previous single-purpose `canvas.click` handler.

| Gesture | Effect |
|---|---|
| **Drag a node** | The node follows the cursor and stays where it is dropped (`n._pinned = true; vx = vy = 0`). The simulation skips physics for pinned and currently-dragging nodes, so the drop position is stable. |
| **Drag an empty area** | Pans the view (`view.tx` / `view.ty`). |
| **Wheel / two-finger scroll / pinch (macOS, via `ctrlKey+wheel`)** | Zooms about the cursor — the world point under the cursor stays under the cursor across zoom. The scale `view.k` is clamped to `[ZOOM_MIN=0.2, ZOOM_MAX=5.0]`. |
| **Hover a node** | Shows a tooltip with the node's display label and member count. The tooltip element is built with `createElement` + `textContent` only — no unsafe DOM-assignment sinks, no `eval`. Tooltip is `position:fixed; pointer-events:none; z-index:9999` so it never inherits a transform / scroll context. |
| **Click a node** (moved <4px AND held <250ms after `pointerdown`) | Focuses the node (persistent ring) and applies the existing `applyCrossFilter` to the Memory tab, scoped to that domain's `member_ids` — the SAME function the [domain sidebar row click](#domain-sidebar-memory-tab) uses. |
| **Click an empty area** | Clears the focus ring and hides the tooltip. |
| **Esc** | If the Memory search input is focused, blur it. Otherwise: clear focus + hover, hide the tooltip, clear the active cross-filter via `clearFilter()`, return to the Graph tab, reset the sidebar selection to the pinned "All memories" row, and unpin every node so the simulation re-settles. |

The simulation loop uses `requestAnimationFrame` + a `dirty` flag. The
`ensureRunning()` / `markDirty()` pair is the only entry point — every
interaction (pointer, wheel, Esc, sidebar click) calls `markDirty()` to wake
the loop. The loop self-stops when `!dragging && !panning && !dirty &&
maxVelocity < 0.05` after `>200ms` idle, so a graph at rest costs zero
animation frames.

> The graph subsystem is implemented entirely inside the existing IIFE in
> `core/templates/ui.html`. The 49-node / capped-edge data path, the
> `build_graph_payload` reuse (#68), and the edge-density banner are
> unchanged.

## Packaging

The template ships in the wheel via the existing
`[tool.setuptools.package-data] core = ["templates/*.html"]` glob and is loaded
through `importlib.resources` — the same pattern `#68`/`#80` use. A packaging
regression test asserts both that `core/templates/ui.html` is in the wheel and
that the wheel metadata declares the optional extra
(`Provides-Extra: ui` + `Requires-Dist: pywebview...; extra == "ui"`), proving
the extra ships without installing pywebview.
