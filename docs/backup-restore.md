# Backup & Restore — Operator Guide (issue #75)

mnemos ships an explicit `backup` / `restore` pair on top of the implicit
continuous backup that [remote sync](remote-sync.md) already produces.  This
guide explains what gets backed up, where archives land, when to run the
commands, how to restore (including the multi-host scenario), and the known
limits that fall out of the design.

This document is the missing operator guide referenced by the **Backup**
section of `docs/remote-sync.md` and by the README's *Remote sync* paragraph.

---

## TL;DR

```bash
# Take an explicit snapshot.
mnemos backup
# → /Users/you/.mnemos/backups/20260529T073900Z.tar.gz

# Take a snapshot at a custom path (e.g. before a risky migration).
mnemos backup --output /tmp/pre-migration.tar.gz

# Restore into the active repo (default: skip on id conflict).
mnemos restore --input /tmp/pre-migration.tar.gz
# → restored: 42  skipped: 0  overwritten: 0

# Force overwrite on id conflict (DANGEROUS — replaces existing files).
mnemos restore --input /tmp/pre-migration.tar.gz --overwrite
```

Both commands resolve the repo root from `MNEMOS_REPO_ROOT` the same way
every other mnemos CLI does.  `mnemos backup` creates the default
`~/.mnemos/backups/` parent directory on first use.

---

## Dual-track model: continuous vs. explicit

mnemos has **two** complementary durability tracks.  Both are first-class and
neither replaces the other.

| Track | Mechanism | Cadence | Channel | Purpose |
|---|---|---|---|---|
| **Continuous** | `mnemos sync` (git-backed) — see [remote sync](remote-sync.md) | Per capture | `git push` to a remote | Distribution + disaster recovery via the remote |
| **Explicit**   | `mnemos backup` / `mnemos restore` (issue #75) | On demand | Single `.tar.gz` archive (any storage you control) | Portable, offline-keepable, point-in-time snapshots |

The continuous track is the day-to-day flow: every capture is committed and
pushed in lockstep, so a fresh checkout of the remote already reconstructs the
memory store.  The explicit track exists for the cases that `git push` cannot
serve well:

- **Air-gapped snapshots** — hand off an archive to a host that cannot reach
  the git remote.
- **Pre-migration safety net** — `mnemos backup` before running schema
  migrations, large GC sweeps, or experimental promote/demote campaigns.
- **Long-term cold storage** — archive to a backup volume independent of git
  hosting; you can keep an archive even after the remote is deleted.
- **Multi-host hand-off** — move the persistent layers between two hosts that
  do NOT share a git remote (see [Multi-host scenario](#multi-host-scenario)
  below).

Do **not** treat the remote git repository as a substitute for explicit
backups (and do not treat explicit backups as a substitute for the remote).
Each track owns its own failure modes.

---

## What is backed up

A backup is a single gzip tar archive that contains exactly two things:

1. **`manifest.json`** at the archive top level — see
   [Manifest schema](#manifest-schema) below.
2. **`wiki/<layer>/*.md`** entries for every persistent layer named in
   `core.layers.LAYER_STATIC_PATHS`:
   - `wiki/projects/*.md`
   - `wiki/global/*.md`
   - `wiki/entities/*.md`
   - `wiki/claims/*.md`
   - `wiki/topics/*.md`

Every memory item under those layers preserves its YAML front-matter
intact, so all documented metadata fields round-trip byte-equal:

- `id`
- `tag` (list)
- `layer`
- `trust_level`
- `quality_score`
- `lifecycle_action`
- `created_at`

…plus the markdown content body.

### What is **not** backed up — by design

| Excluded | Why |
|---|---|
| `.agent/` (ephemeral / working / session / transient) | Designed to be short-lived. GC collects these on a 1-hour staleness window; restoring them would resurrect data the operator has already chosen to discard. Matches the sync wiki-only stage filter. |
| `wiki/policy.yaml`, `wiki/log.md`, `wiki/log.jsonl`, `wiki/index.md`, `wiki/observability.jsonl` | These are control / audit artefacts, not memory items. Restoring them would overwrite the *destination* host's local policy + audit log. |
| `.agent/state/fts.db` (FTS5 index) | Rebuildable from the restored memory items. Re-running `mnemos search` for the first time after restore rebuilds the index on demand. |

This is the **same** exclusion list the continuous git-sync track honours, so
the two tracks have identical surface area — switching between them does not
change what survives.

---

## Where backups land

| Mode | Path |
|---|---|
| Default (no `--output`) | `~/.mnemos/backups/<UTC timestamp>.tar.gz` |
| Custom (`--output PATH`) | exactly `PATH` |

The CLI creates `~/.mnemos/backups/` on first use.  The timestamp format is
`YYYYMMDDTHHMMSSZ` (UTC, no separators) so archives sort
lexicographically by age.

For a custom `--output`, **the caller owns the parent directory**: if it does
not exist, the command exits non-zero with a clear error.  This prevents
silently creating archives in an unexpected location.

---

## When to back up

There is **no automatic schedule.**  Operators run `mnemos backup` manually
(or via their own cron).  Recommended triggers:

- Before any **schema migration** (`mnemos migrate`, `mnemos memory-validate
  --fix`, manual front-matter edits).
- Before a **large GC pass** that may archive or forget many items.
- Before **switching backends** (default ↔ Obsidian) so you can roll back the
  ingest cleanly.
- Before **switching hosts** (laptop → workstation, host A → host B).
- On a **regular cadence** that matches your acceptable data-loss window
  (daily / weekly).  This is in addition to — not a replacement for — the
  remote push that happens on every capture.

Restoring **never** runs automatically: it is always an explicit operator
action.

---

## Restore semantics

The default policy is **skip on id conflict**:

- If the destination already has a file at the target path, the archive entry
  is skipped and counted under `skipped`.
- Existing files are left untouched.
- The CLI prints `restored: X  skipped: Y  overwritten: Z`.

Pass `--overwrite` to flip the policy to **overwrite on id conflict**:

- The existing file is replaced.
- The replacement is counted under `overwritten`.
- The skipped bucket stays at 0.

`--overwrite` is the dangerous mode.  Use it only when you have **deliberately
chosen** to discard the destination's current state for the conflicting ids
(e.g., restoring a known-good snapshot after a bad merge).

### Multi-host scenario

The canonical hand-off flow between two hosts that do not share a git remote:

```bash
# --- Host A ---
mnemos backup --output /shared/transfer/host-a-2026-05-29.tar.gz

# --- Host B (after copying the archive via your transport of choice) ---
mnemos restore --input /shared/transfer/host-a-2026-05-29.tar.gz
```

Host B's `mnemos search` works against the restored items immediately
(the first query rebuilds the FTS index on demand).  Round-trip fidelity:
every documented metadata field survives byte-equal; only absolute paths
differ (because the archive stores paths relative to repo root).

When the two hosts already share a remote, the **continuous track** is the
better hand-off: a push on A + a pull on B does the same work with less
operator ceremony.

---

## Manifest schema

The top-level `manifest.json` carries the snapshot's identity and integrity
metadata:

```json
{
  "schema_version": 1,
  "source_host": "host-a.example.com",
  "generated_at": "2026-05-29T07:39:00Z",
  "item_count": 42,
  "layer_summary": {
    "project": 12,
    "global": 4,
    "entities": 18,
    "claims": 6,
    "topics": 2
  }
}
```

| Field | Meaning |
|---|---|
| `schema_version` | Frozen contract.  Restore refuses an archive whose `schema_version` is not the version this build supports (`ValueError`).  Bumped only on a backward-incompatible change. |
| `source_host` | `socket.gethostname()` of the host that produced the archive.  Informational. |
| `generated_at` | UTC ISO 8601 with `Z` suffix.  Identifies the snapshot in time. |
| `item_count` | Total number of memory items in the archive. |
| `layer_summary` | Per-layer count of items.  Useful for sanity checking before a restore. |

You can read the manifest **without unpacking the archive** via
`core.backup.read_backup_manifest(archive_path)` (or, for an ad-hoc CLI peek,
`tar -xzOf <archive> manifest.json | jq .`).

### Forward-compatibility guard

```python
>>> from core.backup import restore_backup
>>> restore_backup("/tmp/from-future.tar.gz", "/repo")
ValueError: Archive /tmp/from-future.tar.gz declares schema_version=42; this
build only restores schema_version=1.
```

The CLI surface mirrors the same error: `mnemos restore --input
/tmp/from-future.tar.gz` exits 1 with the message above on stderr.

---

## Safety guarantees

- **Path-traversal hardened.**  `restore_backup` calls
  `tarfile.extractall(filter="data")` (Python 3.12+) AND independently
  verifies that every member resolves to a path under the destination repo
  root.  A malicious archive with `../etc/passwd` style entries is rejected
  with `ValueError` before any file is written.
- **No host-specific absolute paths in the archive.**  All entries are
  relative to repo root, so an archive from host A restores correctly into
  host B's repo root regardless of where each host's mnemos lives on disk.
- **No new third-party dependencies.**  `core/backup.py` is pure stdlib
  (`pathlib`, `tarfile`, `json`, `dataclasses`, `socket`, `datetime`).  The
  import is cheap and the module has no runtime config.
- **No behavior change to existing commands.**  `backup` and `restore` are
  additive subcommands.

---

## Known limits

These are deliberate design choices rather than bugs.  Each one has a
rationale below.

- **No automatic scheduling.**  mnemos does not ship a cron daemon.  Wire
  `mnemos backup` into your existing scheduler (system cron, launchd,
  systemd timer) when periodic snapshots are desired.  The default-path
  format sorts lexicographically by age, so rotation is a one-line shell
  pipeline.
- **`.agent/` ephemeral data is excluded.**  This matches the sync wiki-only
  stage filter.  If you need to preserve a specific ephemeral capture, copy
  the file out of `.agent/` manually before it ages out.
- **Large repos take linear time.**  The archive is a streaming gzip tar of
  every persistent `.md` file; the work scales linearly with the number of
  items.  For a typical wiki of a few thousand items the operation is
  sub-second; for very large wikis (tens of thousands of items) plan
  accordingly.
- **FTS5 index is not in the archive.**  The index is rebuildable from the
  restored items; the first `mnemos search` on the destination host triggers
  the rebuild.  This keeps archive size bounded and avoids shipping a
  platform-specific SQLite file across hosts.
- **No incremental / differential mode.**  Each archive is a full snapshot.
  Combined with deduplication-friendly gzip output this is usually the right
  trade-off; if true differentials are ever needed, the manifest schema is
  versioned so a future archive layout can add them without breaking older
  tooling.

---

## Related reading

- [Remote sync operator guide](remote-sync.md) — the continuous-track sibling.
- `core/backup.py` — the pure-stdlib reference implementation.
- `tests/test_backup.py` — the executable specification (round-trip,
  multi-host, conflict policy, schema guard, path-traversal hardening, CLI
  surface).
