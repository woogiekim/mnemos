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

### Fixed

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
