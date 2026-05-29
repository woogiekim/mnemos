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

Nothing yet. Add entries here under `Added` / `Changed` / `Deprecated` /
`Removed` / `Fixed` / `Security` as work merges to `main`, then promote them
into a dated version section when the release is cut.

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
