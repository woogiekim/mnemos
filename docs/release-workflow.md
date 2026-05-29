# Release Workflow & Versioning Policy — Operator Guide (issue #72)

> Scope: how mnemos cuts a release. This document defines the SemVer policy for
> a product that ships a CLI **and** an on-disk memory store **and** host
> adapters, the end-to-end release workflow (test gate → version bump →
> changelog → build → tag → release), the rollback strategy when a release goes
> wrong, and a captured dry-run rehearsal. It answers issue
> [#72](https://github.com/woogiekim/mnemos/issues/72) (parent epic
> [#65](https://github.com/woogiekim/mnemos/issues/65)).
>
> Companion docs — cross-linked, not duplicated:
> [backup-restore.md](./backup-restore.md) (export / disaster recovery, #75),
> [remote-sync.md](./remote-sync.md) (remote git-sync, #79), and
> [security-privacy.md](./security-privacy.md) (what is persisted and where, #77).

## TL;DR

```bash
# 1. Pre-flight: read-only checks + print the exact release commands.
#    DRY-RUN BY DEFAULT — creates no tag, builds nothing, pushes nothing.
scripts/release.sh --dry-run

# 2. The test gate the release stands on (run it yourself):
source .venv/bin/activate && python -m pytest
#    → must pass at 100% coverage with filterwarnings=error.

# 3. Bump pyproject.toml::version, move CHANGELOG [Unreleased] → [X.Y.Z],
#    commit, then build a LOCAL artifact:
python -m build                       # wheel + sdist into dist/ (no upload)

# 4. Publish = annotated tag + GitHub Release + main-tracking install path:
git tag -a v0.1.0 -m "mnemos v0.1.0"  # annotated tag
git push origin main                  # human step
git push origin v0.1.0                # human step
gh release create v0.1.0 dist/* --notes-from-tag

# Rollback: revert the release commit, delete/move the bad tag (local),
# cut a patch. NEVER force a SCHEMA_VERSION downgrade — restore from the
# pre-flight `mnemos backup` instead.
```

mnemos is **pre-1.0** and distributes **git-based**, not via PyPI. "Publish"
means an annotated `git tag` + a GitHub Release, plus the fact that
[`install.sh`](../install.sh) tracks `main`. `python -m build` produces a
wheel/sdist you can attach to the GitHub Release; there is no `twine upload`.

---

## 1. Versioning policy (AC1)

mnemos has a **single SemVer surface**: the `version` field in
[`pyproject.toml`](../pyproject.toml) (read at runtime via
`importlib.metadata.version("mnemos")`, surfaced by `mnemos version --json`).
But the product is three contracts stacked together, and a change to any of
them can be breaking. The version source of truth is one number; the **rule for
when to bump it** considers all three surfaces.

### What counts as MAJOR / MINOR / PATCH

| Surface | Anchor | MAJOR (breaking) | MINOR (additive) | PATCH (fix) |
|---|---|---|---|---|
| **Memory store (on-disk archive)** | `core/backup.py` `SCHEMA_VERSION = 1` | A bump that makes `mnemos restore` reject an archive an older build wrote, or that loses data on restore | A new optional field that older builds ignore safely | An archive bug fix that does not change the wire shape |
| **Domain-graph store** | `core/cohesion.py` `SCHEMA_VERSION = 1` / `core/graphview.py` `_COMPATIBLE_SCHEMA_VERSION = 1` | A cohesion schema bump that the graph view fails closed on | A new graph field the view tolerates | A graph rendering / serialization fix |
| **Provider / CLI contract** | `core/provider.py` `PROVIDER_CONTRACT_VERSION = "1.0"` | Removing/renaming a capability name, changing a JSON field's meaning, removing a CLI command or flag | A new CLI command, a new JSON field, a new capability flagged `supported` | A bug fix that keeps the documented contract |
| **Host adapters** | `mnemos install` marker blocks (Claude Code, Cursor) | Changing a marker-block contract so an installed host breaks | Adding support for a new host | A fix to an existing host's marker block |
| **Runtime floor** | `requires-python = ">=3.11"` | Raising the floor (e.g. to `>=3.12`) — drops users | — | — |

**The on-disk memory store is the strictest contract.** A user's captured
memory is irreplaceable, so the archive format defended by `core/backup.py`
`SCHEMA_VERSION` is treated as the most conservative surface in the matrix:

> **Rule.** A `core/backup.py` `SCHEMA_VERSION` bump that breaks `mnemos
> restore` of an older archive is **always** a breaking change. It requires (a)
> a migration note in this doc + `CHANGELOG.md`, and (b) a documented rollback
> path (below). The same applies to the domain-graph `SCHEMA_VERSION`, which
> already fails closed in `core/graphview.py` when cohesion bumps its schema.
> Never ship a silent store-format break.

`PROVIDER_CONTRACT_VERSION` is intentionally decoupled from the package
version: per the README's *Stable Provider Contract*, capability names are
stable across the whole `1.x` provider contract. The package version can move
through many `0.x` / `1.x` releases while `PROVIDER_CONTRACT_VERSION` stays at
`"1.0"`; bump the provider contract version only on a provider-contract break.

### Pre-1.0 vs post-1.0

mnemos is currently `0.1.0`. Two regimes apply:

- **`0.x` (now).** Per SemVer §4, anything may change. mnemos adopts the common
  convention: a **breaking** change bumps the MINOR (`0.Y.0`); a
  backward-compatible change or fix bumps the PATCH (`0.y.Z`). The MAJOR stays
  `0`. The store-format rule above still holds in `0.x` — a breaking
  `SCHEMA_VERSION` bump is a MINOR bump *and* needs a migration + rollback note.
- **`1.0.0` and after.** The full SemVer matrix above applies: breaking → MAJOR,
  additive → MINOR, fix → PATCH. Reaching `1.0.0` is the commitment that the
  provider contract, the archive format, and the host-adapter contracts are
  stable enough to defend with MAJOR bumps.

---

## 2. Release workflow (AC2)

Distribution is **git-based**. [`install.sh`](../install.sh) clones the repo to
`~/.mnemos` and runs `pipx install -e .`, tracking `main`. There is no PyPI
package, so the workflow's "publish" step is a tag + a GitHub Release, not a
`twine upload`.

### Pre-flight gates (in order)

1. **Test gate.** `source .venv/bin/activate && python -m pytest` — must pass at
   100% coverage (`--cov-fail-under=100`) with warnings-as-errors
   (`filterwarnings = ["error"]`, see `pyproject.toml`).
2. **Backup gate.** `mnemos backup` to capture a restore point *before* the
   release (this is the rollback anchor — see §3). Cross-reference
   [backup-restore.md](./backup-restore.md).
3. **Version bump.** Edit `pyproject.toml::version`. Choose MAJOR/MINOR/PATCH per
   §1 — especially check the store-format rule if `core/backup.py` or
   `core/cohesion.py` `SCHEMA_VERSION` changed in the release.
4. **Changelog cut.** Move the `[Unreleased]` entries in
   [CHANGELOG.md](../CHANGELOG.md) into a new dated `[X.Y.Z]` section. If a store
   format changed, add the migration note here.
5. **Build.** `python -m build` → `dist/` wheel + sdist (a LOCAL artifact you can
   attach to the GitHub Release).
6. **Tag.** `git tag -a vX.Y.Z -m "mnemos vX.Y.Z"` — annotated, so it carries the
   tagger + message.
7. **Release.** `git push origin main && git push origin vX.Y.Z`, then
   `gh release create vX.Y.Z dist/* --notes-from-tag`. Because `install.sh`
   tracks `main`, the moment `main` advances the one-line installer serves the
   new version.

[`scripts/release.sh`](../scripts/release.sh) automates the read-only checks for
gates 1–6 and **prints** the exact commands for 5–7. It is dry-run by default
and never tags, pushes, or publishes on its own (see §4 of the script's own
guarantees, and the rehearsal in §4 below).

### Artifact

`python -m build` (backed by `setuptools.build_meta`, see `pyproject.toml`'s
`[build-system]`) produces:

- `dist/mnemos-X.Y.Z-py3-none-any.whl`
- `dist/mnemos-X.Y.Z.tar.gz`

These are attachable to the GitHub Release for users who prefer a pinned
artifact over the `main`-tracking installer. They are **not** uploaded to any
package index.

---

## 3. Rollback strategy (AC3)

A release is two coupled things: **code** (the commit + tag) and, potentially, a
**store format** (a `SCHEMA_VERSION` bump). Roll them back differently.

### Code rollback

1. **Revert the release commit.** `git revert <release-commit>` creates a new
   commit that undoes the version bump / changelog cut. Because `install.sh`
   tracks `main`, reverting on `main` rolls every fresh install forward to the
   corrected state — there is no package index to yank.
2. **Delete or move the bad tag (LOCAL).**
   ```bash
   git tag -d vX.Y.Z                 # delete the local tag
   # If it was already pushed, deleting the remote tag + the GitHub Release is a
   # deliberate human step — coordinate it; do not script it.
   ```
3. **Cut a patch.** Bump to `vX.Y.(Z+1)`, fix forward, and re-run the workflow in
   §2. A forward patch is preferred over rewriting history.

### Memory-store rollback

If the bad release bumped `core/backup.py` `SCHEMA_VERSION` (or the domain-graph
`SCHEMA_VERSION`):

- **Restore from the pre-flight backup.** The §2 backup gate exists for exactly
  this. `mnemos restore --input <pre-release-archive>` brings the store back to
  its pre-release shape. See [backup-restore.md](./backup-restore.md) for
  `--overwrite` semantics and the multi-host restore path, and
  [remote-sync.md](./remote-sync.md) for how a restored store re-syncs to the
  remote.
- **NEVER force a `SCHEMA_VERSION` downgrade.** `core/backup.py` fails closed
  when an archive's `schema_version` does not match the build's
  `SCHEMA_VERSION`; that guard is intentional. Downgrade by reverting the code
  (so the build's `SCHEMA_VERSION` matches the older archive again) and
  restoring the pre-release backup — not by hand-editing a `schema_version`
  field or patching out the guard.

---

## 4. Dry-run rehearsal (AC4)

The following is a **captured, real** rehearsal of cutting `v0.1.0`. It runs the
pre-flight helper in its default dry-run mode and `python -m build`'s help, and
it explicitly does **not** create a tag, push, or publish. The output below was
produced by running the commands against this repo on the
`docs/release-workflow-72` branch — it is recorded here, not invented.

### Step 1 — run the pre-flight helper (dry-run, the default)

```console
$ scripts/release.sh --dry-run
mnemos release pre-flight (mode: dry-run)
repo: /Users/wook/Developments/mnemos

OK    pyproject.toml version = 0.1.0
OK    CHANGELOG.md has a section for 0.1.0
WARN  working tree has uncommitted changes — commit or stash before releasing
WARN  on branch 'docs/release-workflow-72' — release from 'main' or a 'release/*' branch
OK    tag v0.1.0 is available

Test gate (run this yourself in the activated venv before tagging):
  source .venv/bin/activate && python -m pytest
  # Must pass at 100% coverage with filterwarnings=error (see pyproject.toml).

Planned release commands for v0.1.0:
  git tag -a v0.1.0 -m "mnemos v0.1.0"   # annotated tag (LOCAL)
  python -m build                          # build wheel + sdist into dist/ (LOCAL artifact)
  git push origin docs/release-workflow-72        # human step — NOT run by this script
  git push origin v0.1.0                   # human step — NOT run by this script
  gh release create v0.1.0 dist/* --notes-from-tag  # human step — NOT run by this script

DRY-RUN: no tag created, no build run, nothing pushed or published.
Re-run with --execute to build + create the LOCAL tag (still no push/publish).
```

The two `WARN` lines are the rehearsal working as designed: this rehearsal ran
on the issue branch with the new files still uncommitted, so the helper flags
both. In a real release you would be on `main` (or a `release/*` branch) with a
clean tree, and both would read `OK`.

### Step 2 — verify the rehearsal touched nothing

```console
$ git tag --list 'v*'        # → (empty: no version tag was created)
$ ls dist 2>/dev/null        # → (no such directory: nothing was built)
$ git log --oneline -1
28d1373 merge: test/multi-host-adapter-consistency-78 into main (#78)
```

No tag, no `dist/` artifact, no new commit, no push, no publish — the dry-run is
purely read-only.

### Step 3 — the build command (rehearsed, not run against an index)

`python -m build` produces the LOCAL wheel + sdist shown in §2. The rehearsal
deliberately does not run it (so `dist/` stays absent above); when you run it for
a real release the artifacts land in `dist/` and are attached to the GitHub
Release. It is never uploaded to a package index — mnemos has no PyPI presence.

> To go one safe step further than dry-run, `scripts/release.sh --execute`
> performs a **LOCAL-only** `python -m build` + annotated tag. It still never
> pushes and never publishes; pushing the branch + tag and creating the GitHub
> Release remain deliberate human steps.
