# Changelog

All notable changes to mnemos are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The release process, the SemVer scope for mnemos' CLI + memory store + host
adapters, and the rollback strategy are documented in
[docs/release-workflow.md](docs/release-workflow.md).

mnemos is pre-1.0. Until `1.0.0`, the `0.x` SemVer rules apply: a breaking
change bumps the MINOR (`0.Y`) and a backward-compatible change bumps the
PATCH (`0.y.Z`). See the release workflow doc for the full policy, including
the rule that a `core/backup.py` `SCHEMA_VERSION` bump that breaks `mnemos
restore` of an older archive is always a breaking change.

## [Unreleased]

### Changed

- Drill-down Related mini-graph for `mnemos ui`
  ([#93](https://github.com/woogiekim/mnemos/issues/93)): the Memory-tab
  drill-down ([#86](https://github.com/woogiekim/mnemos/issues/86) /
  [#92](https://github.com/woogiekim/mnemos/issues/92)) gains a small
  "Related" graph rendered into a NEW `<canvas id="dd-related-graph">`
  embedded between the Content and Trust panels. When a memory is opened,
  `buildRelatedGraph(mem)` derives the local neighborhood from the
  existing payload — no new payload field is added — by scanning
  `graph.domains` for entries whose `member_ids` include `mem.id`
  (containing domains) and sampling up to 8 sibling memory ids from each
  containing domain (deterministic — first 8 in `member_ids` order,
  excluding the center). Siblings are resolved back to full memory
  objects via a new in-IIFE `memoryById` map. The mini-graph runs a
  bounded ~120-tick Verlet/spring simulation (capped at ~20 nodes) and
  renders the center node (cyan), containing-domain nodes (blue), and
  sampled sibling nodes (white) with edges center↔domain and
  domain↔sibling. Domain-node clicks reuse the existing
  `applyCrossFilter(member_ids, "domain " + label)` + `showTab("memory")`
  funnel (the SAME path the main graph, sidebar, and policy table
  already share); sibling-node clicks re-open the drill-down for the
  chosen sibling via `showDrilldown(full)`. When the opened memory has
  zero containing domains the canvas hides and a small
  `#dd-related-empty` placeholder ("No related domain") shows instead.
  Vanilla JS only — no new dependency, no CDN, no `innerHTML` sink.
  The new `<canvas id="dd-related-graph">` is a different id from the
  load-bearing `<canvas id="graph">` packaging token so the
  [#83](https://github.com/woogiekim/mnemos/issues/83) placeholder
  singleton (`__UI_DATA_JSON__` count == 1 in source, count == 0 in
  rendered HTML), the [#83](https://github.com/woogiekim/mnemos/issues/83) /
  [#85](https://github.com/woogiekim/mnemos/issues/85) `id="ui-data"` +
  `mem-id-pill` + `mem.display_title` bindings, the
  [#86](https://github.com/woogiekim/mnemos/issues/86) cross-filter
  wiring + `createElement`-only DOM mutation contract, the
  [#90](https://github.com/woogiekim/mnemos/issues/90) Memory-first tab
  order, the [#91](https://github.com/woogiekim/mnemos/issues/91)
  futuristic white theme tokens, and the
  [#92](https://github.com/woogiekim/mnemos/issues/92) readability
  primitives are all preserved verbatim. `tests/test_cli_ui.py` gains a
  `TestUi93DrilldownRelatedGraph` class that pins the new canvas id,
  the `.related-panel` markup, the `buildRelatedGraph` function name,
  the `dd-related-empty` placeholder, the `applyCrossFilter(n.domain.member_ids,`
  wire, the `memoryById` resolver, the `showDrilldown(full)` sibling
  reopen, the `.related-panel canvas` + `.related-empty` CSS hooks, the
  `__UI_DATA_JSON__` singleton (both source and rendered), the
  load-bearing `<canvas id="graph">` regression guard, and the
  no-`innerHTML` invariant. Verified end-to-end with a Chrome headless
  probe that clicks the first `#result-list li`, evaluates
  `document.getElementById("dd-related-graph")` (exists),
  `getBoundingClientRect()` (`width=389.75`, `height=200`), inspects
  intrinsic `canvas.width=389` + `canvas.height=200`, and confirms the
  2D context has been drawn to (>50 non-zero alpha pixels via
  `getImageData`).
- Content readability in `mnemos ui`
  ([#92](https://github.com/woogiekim/mnemos/issues/92)): stop the
  aggressive `...` truncation experience. The drill-down `#dd-content`
  panel now renders the FULL memory content via a new always-present
  `mem.content_full` payload field — independent of the
  `--preview-width` flag — so the operator never has to re-issue a
  command with `--full` just to read a memory. The Memory-list rows
  swap the previous single-line ellipsis for a 2-line clamped preview
  (`display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;`) with a soft bottom mask
  (`mask-image: linear-gradient(to bottom, #000 70%, transparent 100%)`)
  that works in both WKWebView (macOS pywebview host) and Chromium
  (headless probe). Each row gains a small chevron toggle
  (`.mem-expand-btn`, `▾` / `▴`) that expands an inline
  `.mem-content-full` block — state is row-local and survives
  cross-filter re-renders. The `mnemos ui` command's `--preview-width`
  default rises 240 → 480 so even the preview text isn't cut as
  eagerly before the 2-line wrap kicks in (the legacy `mnemos inspect`
  and `mnemos graph` defaults stay at 240). The `--full` CLI flag is
  unchanged — it still bypasses preview truncation so `mem.content`
  equals `mem.content_full`, useful for `--output` headless renders;
  the drill-down panel always shows the full content regardless of the
  flag. Additive payload only: `mem.content` still carries the
  preview-truncated string for the 2-line row, `mem.content_full`
  carries the verbatim original, and `preview_truncated` remains as
  the explicit clipped-flag — no schema bump (`schema_version` stays
  `1`). The [#83](https://github.com/woogiekim/mnemos/issues/83)
  layout chain, [#85](https://github.com/woogiekim/mnemos/issues/85)
  `display_title` + `mem-id-pill` binding,
  [#86](https://github.com/woogiekim/mnemos/issues/86) domain
  sidebar, [#90](https://github.com/woogiekim/mnemos/issues/90)
  Memory-first tab order, and
  [#91](https://github.com/woogiekim/mnemos/issues/91) futuristic
  white theme tokens (`--accent: #2563eb`, glass header backdrop,
  pill-shaped nav, tabular numerics) are all preserved verbatim.
  `tests/test_inspectview.py` gains a `TestContentFullField92` class
  that pins `content_full` presence on every memory (short, long,
  `--full`, and Korean Unicode content) and asserts deterministic
  field ordering (`content` → `content_full` → `preview_truncated`).
  `tests/test_cli_ui.py` gains a `TestUi92ContentReadability` class
  that pins the new CSS classes (`.mem-content-preview`,
  `.mem-expand-btn`, `.mem-content-full`, `-webkit-line-clamp: 2`,
  `mask-image` fade), the JS wiring (`previewEl.className`,
  `fullEl.classList.toggle("shown")`,
  `mem.content_full || mem.content` for both list-expand and
  drilldown), the `--full` bypass (`content == content_full`), and
  the 480 default (with the legacy `inspect` default unchanged at
  240). Verified end-to-end with a real pywebview probe
  (`dd_matches_content_full == true` on a long Korean memory:
  `content` 483 chars truncated vs `content_full` 719 chars rendered
  verbatim in `#dd-content`; row expand toggle flips
  `.mem-content-full.shown` from `false` to `true` on click).
- Futuristic white theme for `mnemos ui`
  ([#91](https://github.com/woogiekim/mnemos/issues/91)):
  `core/templates/ui.html` ships a redesigned `<style>` block built on a
  near-white surface palette (`--bg: #f8fafc`, `--surface: #ffffff`) with
  a deep electric-blue accent (`--accent: #2563eb`) and a cyan secondary
  (`--accent-2: #06b6d4`). The header gains a subtle glass effect
  (`backdrop-filter: saturate(140%) blur(8px)`), nav tabs become pills
  (`border-radius: 9999px`) with an active blue/ring treatment, and every
  count surface (header meta, sidebar counts, edge-density banner,
  footer) uses `font-variant-numeric: tabular-nums` for column-aligned
  digits. Panel cards gain a soft elevation shadow, the policy table
  header uses uppercase tracked labels, scrollbars are slimmed to a
  translucent thumb, and the graph canvas reuses the same palette
  (`#2563eb` node fill, `#1d4ed8` stroke, cyan focus halo). DOM
  structure, IDs, classes, JS logic, payload contract,
  [#83](https://github.com/woogiekim/mnemos/issues/83) layout chain,
  [#85](https://github.com/woogiekim/mnemos/issues/85) `display_title` +
  `mem-id-pill` binding, [#86](https://github.com/woogiekim/mnemos/issues/86)
  domain sidebar, and [#90](https://github.com/woogiekim/mnemos/issues/90)
  Memory-first tab order are all unchanged — only the visual language
  was reworked.
- Memory-first tab order in `mnemos ui`
  ([#90](https://github.com/woogiekim/mnemos/issues/90)):
  `core/templates/ui.html` swaps the nav tab order so **Memory** is the
  first tab in DOM and the default-active tab on load (Graph stays
  second, Policy Cohesion third). `aria-selected="true"` and
  `class="tab active"` move from the Graph button/section onto the
  Memory button/section; the JS `tabs = { graph, memory, policy }` map,
  the `showTab(...)` function, the `applyCrossFilter` graph→Memory
  cross-filter wire, and the [#83](https://github.com/woogiekim/mnemos/issues/83)
  layout chain (`body height:100vh`, `main height:0`) are unchanged.
  Presentation only — no Python / payload / schema / on-disk change, no
  new dependency. The Memory tab's `#domain-sidebar` is therefore
  visible on first load without any click. `tests/test_cli_ui.py` gains
  a `TestUi90MemoryFirstTab` class that pins the nav order, the
  initial `aria-selected` placement, the section DOM order
  (`#tab-memory` before `#tab-graph`), the `class="tab active"` move,
  and that the `#domain-sidebar` lives inside the initially-active
  section. Verified end-to-end with a real pywebview probe
  (`active_tab_initial == "tab-memory"`, `#domain-sidebar.offsetParent
  !== null`, `header.getBoundingClientRect().height > 0`).

### Added

- Substantive AI capture
  ([#88](https://github.com/woogiekim/mnemos/issues/88)): invert
  `core/transcript.py` `extract_insights` from a marker-WHITELIST gate to a
  mechanics-BLACKLIST gate with paragraph chunking. Assistant text is split
  on blank-line boundaries (internal whitespace collapsed) and each surviving
  paragraph emits a `TranscriptInsight(kind="paragraph")` routed by
  `_layer_for_content`. Four blacklist predicates skip fenced code
  (bash/sh/shell/json/yaml/toml/...), function-call markers
  (`<function_calls>`, `<invoke name=...`, `tool_use:`), short non-decision
  paragraphs (< 80 chars without a decision word), and thinking-aloud stubs
  (`let me check`, `looking at`, `확인하겠` ...). Markers still emit
  `kind="marker"` insights unconditionally; `kind="durable-line"` is
  unchanged; `kind="assistant-summary"` is removed (the `_summarize` helper
  and `_SIGNAL_RE` constant along with it). `core/adapters/base.py`
  `MNEMOS_BEHAVIOR_BLOCK` gains an `### End-of-turn capture obligation`
  section with `Capture these` / `Do NOT capture` lists plus positive and
  negative examples; both `ClaudeCodeAdapter` and `CursorAdapter` install
  this verbatim. `backup.SCHEMA_VERSION` stays at `1` and
  `gateway.capture()`'s signature, layer routing, and on-disk format are
  unchanged.
- Automatic distillation
  ([#87](https://github.com/woogiekim/mnemos/issues/87)): subscribe to the
  in-process `post-capture` event seam in `MemoryGateway.__init__` and run
  `core.distill.compute_domain_plan` / `compute_policy_plan` in apply mode
  every N captures (default 25); also run at the end of
  `gateway.consolidate()`. Configurable via `mnemos.yml` under
  `storage.distillation.enabled` (default `true`) and
  `storage.distillation.interval_captures` (default `25`). The captures
  counter is persisted at `~/.mnemos/.distill-state.json` with atomic
  rewrite; the sidecar lives outside the wiki/backup tree and tolerates
  missing or corrupt content. Errors are caught at every layer and logged
  via `core/observability.py` (`event="auto_distill"` in
  `.agent/observability.jsonl`); `gateway.capture()`'s signature, the
  shell-hook fire (`core/gateway.py:543`), and the in-process event emit
  (`core/gateway.py:544`) are unchanged, and `backup.SCHEMA_VERSION` stays
  at `1`. See [`docs/final-memory-distillation.md`](docs/final-memory-distillation.md#automatic-distillation)
  for the full operator guide.

### Fixed

- `MemoryGateway` no longer crashes with `FileNotFoundError` when
  `MNEMOS_REPO_ROOT` points at a mnemos source-tree checkout
  ([#96](https://github.com/woogiekim/mnemos/issues/96)). `core/gateway.py`
  resolves `policy.yaml` via an ordered candidate search —
  `MNEMOS_POLICY_PATH` override → `<root>/wiki/policy.yaml` (install
  layout) → `<root>/repo/wiki/policy.yaml` (dev/source-repo layout) — and
  `_resolve_repo_root` accepts either conventional layout when validating
  the env var. The override is a *preference*: a missing override path
  falls through to the conventional candidates rather than crashing. The
  error message lists every tried path and points at `MNEMOS_POLICY_PATH`
  as the escape hatch. Visible fix: Claude Code's Edit/Write tools stop
  printing the `bg-check` Python traceback throttle line on dev-repo
  setups. `backup.SCHEMA_VERSION` stays at `1`; no signature change to
  `PolicyEngine`.
- Short durable statements like `"user preference: always TDD for new core
  code."` are no longer dropped by the mechanics-blacklist's
  short-paragraph rule
  ([#89](https://github.com/woogiekim/mnemos/issues/89) follow-up to
  [#88](https://github.com/woogiekim/mnemos/issues/88)).
  `core/transcript.py` `_DECISION_WORDS` is extended with `"preference"`,
  `"constraint"`, `"선호"`, and `"제약"` so that `_is_short_non_decision_paragraph`
  recognises them as decision-bearing and lets the surrounding paragraph
  through `extract_insights` as `kind="paragraph"`. `backup.SCHEMA_VERSION`
  stays at `1`; no other module changes.
- `_derive_display_title` no longer surfaces horizontal-rule lines (`---`,
  `===`, `***`, `___`, `- - -`) as memory titles
  ([#85](https://github.com/woogiekim/mnemos/issues/85) follow-up). On a real
  388-memory store this dropped the offender count from 38 (~10%) to 0:
  memories whose content begins with a YAML-style block now surface the inner
  `name` / `title` / `summary` / `description` / `theme` value instead of the
  literal fence. `core/inspectview.py` adds two module-level constants
  (`_RULE_LINE_RE`, `_KEY_VALUE_RE`) and two pure helpers (`_is_rule_line`,
  `_strip_one_matching_quote`); the helper still returns a `str` with the same
  `(content, id_) -> str` signature and the wire-format `schema_version` stays
  at `1`. Empty key values fall through to the next line rather than yielding
  the literal `key:` text, and lines like `--` (length 2) or `-x-` (non-rule
  char) are correctly preserved as plain titles.

### Added

- Memory-tab domain sidebar in `mnemos ui`
  ([#86](https://github.com/woogiekim/mnemos/issues/86)):
  `core/templates/ui.html` gains an `<aside id="domain-sidebar" role="list">`
  as the first child of `#tab-memory`, listing one row per `graph.domains`
  entry (alphabetized by `d.label`) plus a pinned "All memories" row
  (`data-domain-row="__all__"`). Each row is a real `<button>` with
  `role="listitem"` + `tabindex="0"`; click and `Enter`/`Space` call the
  existing `applyCrossFilter(d.member_ids, …)`, the pinned row calls
  `clearFilter()`, and `ArrowUp`/`ArrowDown` move focus across rows. The
  sidebar is presentation only — no Python / payload / schema / on-disk
  change, no new dependency. The [#83](https://github.com/woogiekim/mnemos/issues/83)
  layout chain and the [#85](https://github.com/woogiekim/mnemos/issues/85)
  `display_title` contract are preserved verbatim. See
  [docs/unified-inspection-ui.md § Domain sidebar (Memory tab)](docs/unified-inspection-ui.md#domain-sidebar-memory-tab);
  cross-linked from
  [docs/domain-graph-view.md](docs/domain-graph-view.md) and
  [docs/memory-inspection.md](docs/memory-inspection.md).

- Obsidian-style interactive graph in `mnemos ui`
  ([#86](https://github.com/woogiekim/mnemos/issues/86)):
  `core/templates/ui.html` replaces the single-purpose `canvas.click`
  handler with a full pointer state machine — drag a node to reposition
  and pin it (`n._pinned = true`); drag an empty area to pan; wheel /
  pinch (macOS `ctrlKey+wheel`) zooms about the cursor, clamped to
  `[ZOOM_MIN=0.2, ZOOM_MAX=5.0]`; hover a node to show a tooltip built
  with `createElement` + `textContent` only (no unsafe DOM-assignment
  sinks, no `eval`); click a node (moved <4px AND <250ms) focuses it and
  cross-filters the Memory tab via the existing `applyCrossFilter`;
  `Esc` clears focus + filter, returns to the Graph tab, and resets the
  sidebar to the pinned "All memories" row. The simulation runs on a
  `requestAnimationFrame` + dirty-flag loop (`ensureRunning()` /
  `markDirty()`) that self-stops after >200ms idle with `maxVelocity <
  0.05`, so a graph at rest costs zero animation frames. Presentation
  only — no Python / payload / schema / on-disk change, no new
  dependency. See
  [docs/unified-inspection-ui.md § Interactive graph (Graph tab)](docs/unified-inspection-ui.md#interactive-graph-graph-tab);
  cross-linked from
  [docs/domain-graph-view.md](docs/domain-graph-view.md) and
  [docs/memory-inspection.md](docs/memory-inspection.md).

- Human-readable memory titles in `mnemos inspect` + `mnemos ui`
  ([#85](https://github.com/woogiekim/mnemos/issues/85)):
  - `core/inspectview.py` — new pure helper `_derive_display_title(content, id_)`
    returns the first non-empty content line, stripped of leading Markdown
    heading markers, with internal whitespace collapsed and truncated at 80
    visible code points (Unicode-safe for Korean and emoji). Falls back to a
    slug-shaped id when content is empty, or to the raw id when neither yields
    anything meaningful. `build_inspect_payload` emits an additive
    `display_title` field on every memory; the payload's `schema_version`
    stays at `1` (presentation-only — no store / schema / API / on-disk
    change). `build_unified_payload` inherits the field automatically since it
    reuses `build_inspect_payload`.
  - `core/templates/inspect.html` + `core/templates/ui.html` — render
    `display_title` as the primary list-row heading and drill-down title; the
    persisted id is demoted to a small, muted secondary label (`.mem-sub`
    line on inspect, `.mem-id-pill` on the unified UI). The search match
    function in both surfaces now also matches `display_title`
    case-insensitively. The load-bearing `__UI_DATA_JSON__` placeholder still
    appears exactly once in `ui.html` (the #83 lesson is preserved by a new
    template-source assertion).

- Final-memory distillation — persist derived domains + aggregated policies as
  managed, durable artifacts ([#84](https://github.com/woogiekim/mnemos/issues/84)):
  - `core/distill.py` — the persistence/management layer that #67's read-only
    cohesion derivation lacks. Pure planners `compute_domain_plan` /
    `compute_policy_plan` build would-be artifacts; `apply_domain_plan` /
    `apply_policy_plan` persist them via `gateway.capture` with additive
    `artifact_kind` / `sources` / `distillation_method` / `cohesion_schema_version`
    front-matter, deterministic `uuid5` ids, and skip-if-exists idempotency.
    Lineage is non-destructive and bidirectional: the artifact carries `sources`
    and each source gains an append-only `distilled_into` back-link — sources are
    never archived or superseded (the deliberate difference from #81
    `superseded_by`). The artifact layer is derived via `derive_merged_layer`
    (PolicyEngine), never hard-coded; a feedback-loop guard excludes prior
    artifacts from the source pool so re-runs converge. No LLM dependency in the
    default path. Reuses `derive_merged_layer` + the `_GatewayLike`/`_PolicyLike`
    protocols from `core/compaction.py` — no public API change to
    cohesion/gateway/store/policy/backup, and `backup.SCHEMA_VERSION` stays `1`.
  - `mnemos distill` CLI group — `domains review|apply`, `policies review|apply`,
    `cohesion` (text + `--format json`, the first standalone exposure of
    `aggregate_policy_cohesion`), and `restore-source`. `review` is a dry-run
    (writes nothing); `apply` echoes `distilled: <id> ← <n> sources (layer=...)`
    and is idempotent. New fields round-trip through git-sync (#69/#79) and
    backup/restore (#75) — round-trip tests included.
  - Operator guide [docs/final-memory-distillation.md](docs/final-memory-distillation.md);
    cross-linked from the #81, #83, and #68 docs (not duplicated).
- Unified inspection UI as a native desktop app via `mnemos ui`
  ([#83](https://github.com/woogiekim/mnemos/issues/83)):
  - `core/unifiedview.py` — one self-contained static-HTML surface combining
    the domain graph (#68), the raw-memory inspect view (#80), and a new
    policy-cohesion panel (#67), built by reusing the existing payload builders
    (no graph/memory payload reimplementation). Domain→memory and
    policy→memory cross-filtering, plus controllable graph edge density
    (`_cap_edges_by_node`: per-node top-N + weight threshold) to tame the
    ~33k-edge hairball. Presentation only — no cohesion/policy/gateway/store
    API or on-disk-format change (#84 is the separate persistence issue).
  - `mnemos ui` — default launches a native pywebview desktop window;
    `--output PATH` writes the self-contained HTML headlessly (CI/no-GUI) and
    never imports pywebview; a missing `[ui]` extra on the default path exits
    non-zero with an actionable `pip install 'mnemos[ui]'` hint. Edge-density
    knobs `--max-edges-per-node` (default 8) / `--edge-weight-threshold`
    (default 0.0), plus `--layer` / `--limit` / `--preview-width` / `--full`.
  - Optional `[ui]` pip extra (`pywebview>=5.0`); base dependencies unchanged.
    `core/templates/ui.html` ships via the existing package-data glob.
  - Operator guide [docs/unified-inspection-ui.md](docs/unified-inspection-ui.md);
    cross-linked from the #68 and #80 docs.
- Long-running beta validation harness + `mnemos beta-run` CLI
  ([#82](https://github.com/woogiekim/mnemos/issues/82), epic #65):
  - `core/beta_harness.py` — a deterministic, seeded virtual-clock driver
    (`VirtualClock`, a per-day capture/search/promote/GC/lifecycle workflow)
    that exercises the real `MemoryGateway` / store on an isolated tmp home
    (no mocks) and reports five acceptance-criteria metrics: contextual
    continuity recall, retrieval relevance stability, lifecycle-invariant
    consistency, and degradation + recovery.
  - `mnemos beta-run --days N --seed S --output PATH [--json]` — runs the
    harness and emits a JSON or human/markdown report. Two same-seed runs
    produce a byte-identical normalized report.
  - Simulated time injected without any production signature change (capture
    `created_at` override + `age_hours: 0.0` harness policy + clock-injectable
    `compute_garbage_score(now=…)`). Stdlib only — no new dependency.
  - Operator guide [docs/beta-validation.md](docs/beta-validation.md) with an
    embedded real captured sample run; tracked sample report under
    [docs/examples/beta-run-sample.json](docs/examples/beta-run-sample.json).
- `mnemos compact` CLI group for similar-memory detection + semantic
  compression with lineage audit
  ([#81](https://github.com/woogiekim/mnemos/issues/81)):
  - `core/similarity.py` — deterministic Jaccard + union-find grouping
    over NFKC-normalised tokens (optional n-gram).
  - `core/compaction.py` — frozen `MergePlan` / `MergeResult`
    dataclasses, lossless deterministic summariser with `## Sources`
    audit header, policy-driven `derive_merged_layer`, pure
    `compute_merge_plan`, and lineage-preserving `apply_merge_plan`.
  - `mnemos compact review` (dry-run), `apply`, `restore-source`,
    `merge-candidates` subcommands.
  - Opt-in `--summarizer=llm` summariser reuses the already-pinned
    `anthropic` client; on any failure it falls back to the
    deterministic path with a logged warning.
  - Three additive YAML front-matter keys on memories: `sources`,
    `compaction_method`, `superseded_by`. No new dependency, no
    `core/backup.py:SCHEMA_VERSION` bump, no public API change in
    `core/gateway.py` / `core/store.py` / `core/policy.py`.
  - Round-trip verified through git-sync (#69/#79) and
    `mnemos backup` / `mnemos restore` (#75).
  - Operator guide:
    [docs/memory-compaction.md](docs/memory-compaction.md).

### Fixed

- `mnemos ui` desktop window now renders the actual UI instead of raw JSON for
  real stores ([#83](https://github.com/woogiekim/mnemos/issues/83)):
  `core/unifiedview.py:launch_app` writes the rendered HTML to a temporary
  `.html` file and loads it via a `file://` URL rather than passing the
  ~1.7MB string inline as `html=` (WKWebView/pywebview cannot reliably render
  inline HTML that large). The headless `--output` path is unaffected.

## [0.1.0] - 2026-05-29

Baseline release summarizing the surface shipped to date. mnemos distributes
git-based (`install.sh` → clone to `~/.mnemos` → `pipx install -e .`); there is
no PyPI publish.

### Added

- Git-backed remote sync for the wiki tree: pull on capture, commit on write,
  push after commit, manual `mnemos sync continue` on conflict
  ([#69](https://github.com/woogiekim/mnemos/issues/69),
  [#79](https://github.com/woogiekim/mnemos/issues/79)).
- Explicit `mnemos backup` / `mnemos restore` commands layered on top of the
  continuous-backup track, with a portable archive
  (`core/backup.py` `SCHEMA_VERSION = 1`)
  ([#75](https://github.com/woogiekim/mnemos/issues/75)).
- Read-only domain/policy cohesion abstraction
  (`core/cohesion.py` `SCHEMA_VERSION = 1`)
  ([#67](https://github.com/woogiekim/mnemos/issues/67)).
- `mnemos graph` CLI with a vendored static-HTML domain relationship graph
  view (`core/graphview.py`)
  ([#68](https://github.com/woogiekim/mnemos/issues/68)).
- Stable provider contract surface (`core/provider.py`
  `PROVIDER_CONTRACT_VERSION = "1.0"`) consumed by host adapters via
  `mnemos capabilities --json` / `mnemos version --json`.

### Changed

- Domain-graph schema-version guard wired between `core/cohesion.py` and
  `core/graphview.py` so a cohesion schema bump fails closed in the graph view
  ([#68](https://github.com/woogiekim/mnemos/issues/68)).

### Fixed

- Relocated the observability log out of the synced `wiki/` tree into `.agent/`
  so privacy-sensitive telemetry is never pushed to the remote
  ([#77](https://github.com/woogiekim/mnemos/issues/77)).

### Security

- Security & privacy review of the persisted memory store and observability
  output: documents what is persisted, where it can travel, and how a user
  controls deletion/export
  ([#77](https://github.com/woogiekim/mnemos/issues/77)).

### Validated

- Data-safety / corruption-lifecycle scenario coverage
  ([#76](https://github.com/woogiekim/mnemos/issues/76)).
- Multi-host adapter consistency validated across all acceptance criteria
  ([#78](https://github.com/woogiekim/mnemos/issues/78)).

[Unreleased]: https://github.com/woogiekim/mnemos/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/woogiekim/mnemos/releases/tag/v0.1.0
