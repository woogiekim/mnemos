# Remote Sync — Operator Guide

`mnemos` ships an end-to-end git-backed sync layer that keeps a wiki
directory tree in lockstep with a single remote git repository.  This
document is the operator-facing reference: setup, normal flow,
conflict resolution, backup hand-off, and known limits.

The engine (`core/sync.py` — `GitSyncEngine`) was introduced by
[issue #69](https://github.com/woogiekim/mnemos/pull/69).  The
hardening scenarios that validate it end-to-end live in
`tests/test_sync_hardening_79.py` ([issue #79](
https://github.com/woogiekim/mnemos/issues/79)).

---

## Setup

### One-time bootstrap

```bash
mnemos sync init --remote <url> [--branch main]
```

`sync init` is **idempotent**.  It performs four steps against the
backend-selected vault root:

1. `git init` (no-op if the vault is already a repo).
2. `git remote add origin <url>` (or `set-url` if `origin` already
   exists with a different URL).
3. `git fetch origin` (best-effort — a failed fetch logs a warning
   but does NOT abort `sync init`).
4. `git branch --set-upstream-to origin/<branch>` (best-effort — if
   the branch does not yet exist on the remote, push once to create
   it; `sync init` prints a one-line note and exits 0).

`sync init` operates on:

- The **Obsidian vault path** when `storage.backend: obsidian` and
  `storage.vault_path` are both set in `mnemos.yml`.
- The **repository root** (`MNEMOS_REPO_ROOT` or the current
  directory) when the default `MemoryStore` backend has
  `storage.sync.enabled: true` set explicitly.

When neither precondition holds, `sync init` exits non-zero with:

```
error: mnemos sync init requires storage.backend: obsidian in mnemos.yml,
or storage.sync.enabled: true for the default backend
```

### Where the configuration lives

All sync configuration lives in a single `storage.sync` block inside
`mnemos.yml` (see `core/config.py — SyncConfig`):

```yaml
storage:
  backend: default               # or "obsidian"
  vault_path: ~/MyVault          # only consumed when backend == obsidian
  sync:
    enabled: true                # see opt-in asymmetry below
    remote: origin               # default: "origin"
    branch: main                 # default: "main"
    mode: auto                   # "auto" (hooks fire) or "manual"
    auto_pull_on_capture: true   # honour Hook1 pre-write pull
    auto_push_after_commit: true # honour Hook3 post-commit push
    pull_rate_limit_seconds: 30  # default window between pulls
```

The remote URL is read **from `git remote`** — `mnemos` does not honour
an environment variable for the remote URL.  Re-point the remote with
`mnemos sync init --remote <new-url>` or by editing the git remote
directly.

### Opt-in asymmetry — default vs Obsidian backend

The two backends differ in how `storage.sync.enabled` defaults:

| Backend  | Default `enabled` | How to turn sync on |
|----------|-------------------|---------------------|
| `obsidian` | **auto-enabled** when `storage.vault_path` is set. | Set `vault_path`.  Sync turns on automatically.  Override with `storage.sync.enabled: false` to opt out. |
| `default`  | **off**           | Must set `storage.sync.enabled: true` explicitly.  The auto-enable rule never fires for the default backend. |

The asymmetry is by design: an Obsidian vault is already a
single-purpose markdown tree, so wiring sync to it is the expected
flow.  The default `MemoryStore` backend lives inside a general-purpose
repository root that may contain code, build artefacts, and other
unrelated files; turning on sync there is an explicit operator
decision.

---

## Normal flow

When `storage.sync.enabled` is true and `mode: auto`, every
`mnemos capture` (or any other mutation that exercises
`MemoryStore.write` / `ObsidianBackend.write`) drives the three
sync hooks in order:

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Hook 1           │    │ Hook 2           │    │ Hook 3           │
│ pull --rebase    │ →  │ commit           │ →  │ push origin      │
│ (pre-write)      │    │ (post-write)     │    │ (post-commit)    │
└──────────────────┘    └──────────────────┘    └──────────────────┘
        ▲                       ▲                       ▲
        │                       │                       │
        │ rate-limited          │ wiki-scoped on        │ honours
        │ (30s default)         │ default backend       │ auto_push_after_commit
```

### Hook 1 — pull (rate-limited)

`GitSyncEngine.hook_before_write` calls
`should_pull()`, which compares `time.monotonic()` to
`last_pull_ts` and the configured window
(`storage.sync.pull_rate_limit_seconds`, default **30 s**).  If the
window has not elapsed, the hook is a no-op; otherwise it runs
`git fetch origin <branch>` followed by `git rebase origin/<branch>`.

Why rate-limit?  A burst of `mnemos capture` calls in quick succession
would otherwise trigger one pull per call, which is wasteful and adds
network latency to every write.  30 s is a balance between staying
fresh and not flooding the remote.

If `mode: manual` or `auto_pull_on_capture: false`, Hook 1 is a no-op.
The user can always force a pull with `mnemos sync pull`, which
bypasses the rate limit but honours local-only mode.

### Hook 2 — commit (wiki-scoped on default backend)

`hook_after_write_item` stages the paths that the backend's
`stage_filter` callback accepts, then commits them with a generated
message (`mnemos: <layer> capture`).  The `stage_filter` is the
mechanism that gives each backend its commit scope:

- **Default `MemoryStore` backend** — the `stage_filter` restricts
  staging to paths under `wiki/`.  Any change outside `wiki/`
  (build outputs, `.agent/` ephemeral state, source code) is invisible
  to Hook 2 and never reaches git.
- **Obsidian backend** — the `stage_filter` defaults to "all changed
  paths" because the vault root is, by definition, the entire content
  scope.

A write that touches **only** filtered-out paths produces no commit —
Hook 2 returns early without an empty commit and Hook 3 does not fire.

### Hook 3 — push

`hook_after_commit(committed=True)` calls
`git push origin <branch>` when `auto_push_after_commit` is true.  The
push is skipped silently when:

- `committed == False` (Hook 2 produced no commit).
- `remote_has_branch == False` (the upstream branch does not exist
  yet — local commits queue and ship on the next `sync_push` after
  the remote catches up).

Failed pushes raise `GitCommandError` (a `RuntimeError` subclass) so
callers can recover; the **local commit is never rolled back** on
push failure.  The queued commit is shipped on the next successful
push.

---

## Conflict resolution path

When `git rebase` fails during Hook 1, the engine raises
`SyncConflictError` and writes an artefact file at the vault root:

```
<vault_root>/_sync_conflict.md
```

The artefact contains the rebase error detail and a four-step
recovery checklist.  Hook 1 stops; Hook 2 and Hook 3 do not fire for
the failing write.  The local capture itself was already written to
disk before Hook 1 ran, so no data is lost.

### Worked example

```bash
# 1. mnemos capture triggers Hook 1.  The pull-rebase finds a divergent
#    commit on the remote that touches the same wiki page.  Hook 1
#    raises SyncConflictError and writes the artefact.

$ mnemos capture --layer global --content "..."
SyncConflictError: rebase failed — see wiki/_sync_conflict.md

# 2. Inspect what conflicts.

$ cat wiki/_sync_conflict.md
# mnemos Sync Conflict
_A ``git fetch`` + ``git rebase`` conflict was detected._
...

$ git status
rebase in progress; onto abc1234
You are currently rebasing branch 'main' on 'abc1234'.
both modified: wiki/global/architecture.md

# 3. Open the conflicted file(s) under wiki/ in an editor.  Delete the
#    `<<<<<<<` / `=======` / `>>>>>>>` markers and choose / merge the
#    correct content.  Then stage the resolved files.

$ $EDITOR wiki/global/architecture.md
$ git add wiki/global/architecture.md

# 4. Continue.

$ mnemos sync continue
[mnemos sync] continue ok
```

### What `mnemos sync continue` does

`GitSyncEngine.sync_continue` is implemented in `core/sync.py`:

1. **Guard** — scans all configured conflict directories for any
   `*.md` file that still contains the `<<<<<<<` marker.  If any
   remain, it raises `SyncConflictError` listing the offending paths
   **before** calling `git rebase --continue`, so a half-resolved
   rebase cannot be inadvertently completed.
2. **Continue** — runs `git rebase --continue` against the live
   repository.  No mocking — the rebase is exercised end-to-end in
   `tests/test_sync_hardening_79.py::TestAC3ConflictContinueRealRebase`.
3. **Cleanup** — deletes the `_sync_conflict.md` artefact and any
   `transient/conflict-*.md` files left from the conflict window.
4. **Reset rate-limit** — updates `last_pull_ts` so a subsequent
   capture does not immediately re-trigger a pull.

If `sync continue` raises again, the user has not resolved every
marker.  Re-open the listed files, remove the remaining markers,
re-stage, and re-run.

---

## Backup

Backup and restore (snapshotting the wiki tree outside the git history,
or rebuilding from an archived snapshot) is a **distinct workstream**
from remote sync — `git push` is the operational distribution channel;
backup is the disaster-recovery channel.  Do **not** rely on remote sync
as a backup substitute.

See the [backup & restore operator guide](backup-restore.md) for the
`mnemos backup` / `mnemos restore` commands, the dual-track model, the
multi-host hand-off scenario, and the manifest schema (issue #75).

In short:

- Treat the remote `origin` as durable distribution storage but not as a
  backup.
- Take explicit `mnemos backup` snapshots on your own cadence for
  disaster-recovery and multi-host hand-off.

---

## Known limits

These are deliberate design choices, not bugs.  Each one has a reason
that the engine surfaces clearly.

### No automatic conflict merge

The engine never attempts a heuristic merge of conflicting content.  A
rebase conflict surfaces `SyncConflictError`, writes the artefact,
and waits for the operator.  Markdown semantics — especially YAML
front-matter, wikilink anchors, and lifecycle metadata — make a
silent auto-merge almost always wrong; surfacing the conflict and
letting the operator decide is the only safe contract.

### Wiki-only commit scope on the default backend

On the default `MemoryStore` backend, Hook 2's `stage_filter` only
admits paths under `wiki/`.  This means:

- `.agent/` (runtime state — FTS index, ephemeral runs, sessions,
  transient/working layers) is **never committed** and is therefore
  never pushed.  These layers are gitignored at the repo root.
- Source code, build outputs, and any other non-`wiki/` paths in the
  same repository are likewise invisible to sync.

The Obsidian backend does not impose this filter because the vault
root **is** the wiki.

### No environment variable for the remote URL

The remote URL is read from `git remote`.  There is no
`MNEMOS_SYNC_REMOTE` env override.  Re-point the remote with
`mnemos sync init --remote <new-url>` (idempotent — it calls
`git remote set-url` under the hood) or with `git remote set-url`
directly.

### Single remote, single branch

The engine pulls from and pushes to exactly one
`(<storage.sync.remote>, <storage.sync.branch>)` pair — defaulting
to `origin/main`.  Multi-remote fan-out and multi-branch sync are
not supported.  Operators who need fan-out can post-process the
pushed commits with their own `git push` step against a second
remote.

### No push retry or queueing

When `git push` fails (network blip, auth failure, remote rejection),
the engine surfaces the underlying `GitCommandError` and stops.  The
local commit persists — the engine does **not** roll it back — and
the next successful `mnemos sync push` ships it without duplication.
There is no built-in retry loop or background queue; the operator
decides when to retry.

This contract is validated by
`tests/test_sync_hardening_79.py::TestAC6PartialFailureRecovery`.

---

## Reference — `mnemos sync` subcommands

| Command | Description |
|---|---|
| `mnemos sync init --remote <url> [--branch main]` | One-time bootstrap: `git init` + remote + fetch + upstream tracking.  Idempotent. |
| `mnemos sync pull` | Manual pull — bypasses the rate-limit window but honours local-only mode. |
| `mnemos sync push` | Manual push — flushes queued local commits to the remote. |
| `mnemos sync status` | Print ahead/behind counts, dirty state, `last_pull_ts`, `last_push_ts`, `sync_enabled`, `sync_remote`, `sync_branch`. |
| `mnemos sync continue` | Resume after manually resolving a rebase conflict (see § Conflict resolution path). |

---

## Test coverage

The hardening scenarios that validate every behavior in this document
live in `tests/test_sync_hardening_79.py`.  Each test runs fully
offline using a local bare git repository and a `file://` URL — no
network access is required.  The PRD-to-test mapping is documented
in `context/test-coverage.md` under the task's state directory.

Run them with:

```bash
pytest tests/test_sync_hardening_79.py -q
```

The full project test suite enforces `--cov-fail-under=100` and
`filterwarnings=["error"]` (see `pyproject.toml`); the hardening
scenarios honour both invariants.
