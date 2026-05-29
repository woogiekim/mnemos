# Install / Update / Rollback Lifecycle — Operator Guide (issue #74)

This guide documents the contract that every mnemos install / update /
rollback flow upholds end-to-end. It is the operator-side companion to
[issue #74](https://github.com/woogiekim/mnemos/issues/74) (parent epic #65)
and the test contract in `tests/test_install_lifecycle_74.py`.

> **Scope:** the three public entry points only —
> `core.install.install`, `core.updater.run_update`,
> `core.uninstaller.run_uninstall`. The legacy module-level helpers
> (`update_settings_json`, `update_claude_md`, `update_cursor_rules` at
> `core/updater.py:210-375`) are retained for back-compat callers and are
> NOT exercised by `run_update`.
>
> **Companion docs — cross-linked, not duplicated:**
> [`docs/backup-restore.md`](backup-restore.md) (memory-store snapshots, #75),
> [`docs/remote-sync.md`](remote-sync.md) (git-backed continuous sync, #79),
> [`docs/release-workflow.md`](release-workflow.md) (the surrounding release
> lifecycle, #72/#73), and
> [`docs/security-privacy.md`](security-privacy.md) (#77).

---

## TL;DR

```bash
# Fresh install (curl|bash entrypoint or local equivalent)
./install.sh
# → wiki/ + .agent/ scaffold, mnemos.yml, six-layer policy.yaml,
#   ~/.claude/{settings.json,CLAUDE.md} hooks + block,
#   ~/.cursor/rules block, ~/.zshrc MNEMOS_REPO_ROOT export.

# Repeated update — idempotent.  Second run is a byte-level no-op.
mnemos update

# Rollback (best-effort host-config restore — see Manual recovery below).
mnemos uninstall      # remove every managed block + hook entry
```

The six lifecycle guarantees below are enforced by automated tests; this
document is the operator-facing description of why each guarantee exists
and how to recover when something goes wrong.

---

## Six lifecycle guarantees (AC1 .. AC6)

Each acceptance criterion below has a corresponding `TestClass` in
`tests/test_install_lifecycle_74.py`. The test class is the canonical
specification — this section is a human-readable summary.

### AC1 — Fresh install produces a complete state machine

`install(path, home=...)` writes the full set of artifacts required for a
working mnemos installation:

- Wiki tree (`wiki/global`, `wiki/projects`, `wiki/entities`, `wiki/claims`,
  `wiki/topics`) and agent tree (`.agent/runs`, `.agent/sessions`,
  `.agent/state`, `.agent/reports`, `.agent/tools`, `.agent/workflows/hooks`,
  `.agent/transient`).
- `mnemos.yml` with the six canonical layers (`transient`, `ephemeral`,
  `working`, `session`, `project`, `global`).
- `wiki/policy.yaml` mirroring the same six layers, plus `forget` and
  `archive` policy sections.
- `AGENTS.md` manifest and a `# mnemos` block appended to `.gitignore`.
- `~/.zshrc` `export MNEMOS_REPO_ROOT="..."` line (with comment header).
- Per-adapter managed blocks: `~/.claude/CLAUDE.md` mnemos-start/end block,
  `~/.claude/settings.json` PostToolUse / UserPromptSubmit / Stop hooks,
  `~/.cursor/rules` mnemos:start/end block.
- `verify_hooks()` returns `(True, [])` on BOTH `ClaudeCodeAdapter` and
  `CursorAdapter`.

Owned by **`TestFreshInstall_AC1`**.

### AC2 — `run_update` refreshes managed blocks to canonical bytes

`run_update(home=..., skip_git_pull=True, skip_pipx=True)` rewrites any
stale managed block to the current canonical content while leaving the
user's prose around the block byte-identical. The orchestrator routes
through `adapter.update(home)` at `core/updater.py:484-497` and explicitly
does NOT call the legacy `update_settings_json` / `update_claude_md` /
`update_cursor_rules` helpers.

Step 4 of the orchestrator runs `migrate_policy_transient`
(`core/install.py:172-205`), which adds the `transient` layer to any
pre-existing `wiki/policy.yaml` that predates that layer.

Owned by **`TestUpdate_AC2`**.

### AC3 — Failed update recovery quarantines per-adapter crashes

When one adapter's `update()` raises, the loop at
`core/updater.py:485-497` catches the exception, emits a warning to
stderr, and continues with the next adapter. The user is never left in a
half-applied state where one host was updated and the other was not even
attempted. A failing `git_pull` aborts BEFORE sync or any adapter runs
(`core/updater.py:428-456`) so a stale source tree cannot leak into the
install directory.

Owned by **`TestFailedUpdateRecovery_AC3`**.

### AC4 — Rollback after install restores user-owned files

Running `run_uninstall(yes=True, home=...)` after `install(...)` returns
every user-owned file (CLAUDE.md prose, settings.json non-mnemos keys and
hooks, cursor rules user prose, `.zshrc` lines outside the mnemos export)
to its pre-install bytes. Only the managed block region is removed.

Owned by **`TestRollback_AC4`**.

### AC5 — User-owned state preservation across the full lifecycle

The "user-owned state" set (see the next section) is preserved across
`install` + N consecutive `run_update` calls. Pre-existing `mnemos.yml`,
`wiki/projects/*.md`, and `.agent/runs/*/scratch/*.md` files are NEVER
clobbered by install or update.

Owned by **`TestUserStatePreservation_AC5`**.

### AC6 — Repeated update runs are byte-identical

Running `run_update` twice in a row on the same environment produces
byte-identical `settings.json`, `CLAUDE.md`, cursor rules, `.zshrc`, and
`wiki/policy.yaml`. The hook list count never grows across repeated
updates; `migrate_policy_transient` returns `False` on the second update.

Owned by **`TestUpdateIdempotency_AC6`**.

---

## Definition — "user-owned state"

The lifecycle contract guarantees byte-level preservation of every item
in the following set. mnemos never reads OR writes any of these surfaces
except through the explicit managed-block boundaries.

| # | Surface | Boundary |
|---|---|---|
| 1 | `~/.claude/CLAUDE.md` prose | Everything outside `<!-- mnemos-start --> ... <!-- mnemos-end -->` |
| 2 | `~/.claude/settings.json` user keys | Every top-level key (`model`, `theme`, `env`, etc.) and every hook entry whose `command` does NOT contain a mnemos token |
| 3 | `~/.cursor/rules` user prose | Everything outside `<!-- mnemos:start --> ... <!-- mnemos:end -->` |
| 4 | `~/.zshrc` user lines | Every line other than the mnemos comment + `export MNEMOS_REPO_ROOT="..."` |
| 5 | `mnemos.yml` (pre-existing) | The full file, when it already exists at install time (`core/install.py:133-136` existence guard) |
| 6 | Memory store contents | Every file under `wiki/` and `.agent/` (only the *empty parent dirs* are created by install; existing content is left in place) |

Tests that verify each surface live in `TestUserStatePreservation_AC5`
and `TestRollback_AC4` of `tests/test_install_lifecycle_74.py`.

---

## Known limitations

These are documented contractual limits — they are explicit design
decisions, not defects. Each one is testable by the test suite and is
recorded here so operators can plan around them.

1. **`_install_zshrc` cannot rewrite an existing `MNEMOS_REPO_ROOT`.**
   The append at `core/install.py:213-218` is guarded by
   `if "MNEMOS_REPO_ROOT" not in existing`. If a previous install left
   the export pointing at the wrong path (for example, an old
   `~/Development/mnemos` checkout that was moved to a new location),
   the user must edit `~/.zshrc` by hand or run `mnemos uninstall`
   followed by `install.sh` to refresh the line.

2. **No atomic-write guarantee in the adapter update path.** Both
   `ClaudeCodeAdapter._update_settings_json`
   (`core/adapters/claude.py:373-443`) and
   `CursorAdapter._update_cursor_rules` (`core/adapters/cursor.py:120-142`)
   call `path.write_text(...)` directly — no tempfile-rename atomicity.
   A SIGKILL between the open and the close can leave a truncated file.
   The per-adapter `try/except` quarantine
   (`core/updater.py:485-497`) prevents cross-adapter spread; re-running
   `mnemos update` on the torn file restores canonical bytes (AC6).

3. **`migrate_policy_transient` writes via plain `open("w")`.**
   The migration step at `core/install.py:201-203` opens `policy.yaml`
   in plain write mode. A crash between the `open` and the `yaml.dump`
   can leave the policy file empty. Re-running `mnemos update` re-creates
   the file with the canonical six layers, but tools that read
   `policy.yaml` between the crash and the recovery `mnemos update`
   will see a zero-byte file.

---

## Manual recovery procedure

When the validated automatic flows are not sufficient, the following
manual steps recover a working installation.

### Host-config rollback — clean slate

```bash
mnemos uninstall
# Removes every managed block + hook entry from:
#   ~/.claude/settings.json
#   ~/.claude/CLAUDE.md
#   ~/.cursor/rules (or rules.md)
#   ~/.zshrc          (mnemos comment + export line)
# Leaves all other content byte-identical (AC4 contract).
```

After `mnemos uninstall` returns `0`, re-run `install.sh` (or the project
checkout's local equivalent) to start fresh.

### Source rollback — pin to a known-good commit

When a recent `mnemos update` pulled in a regression, roll the install
checkout back to a known-good SHA and re-sync the install location
without re-pulling:

```bash
# 1. Identify the last green commit.
cd ~/.mnemos
git log --oneline -20

# 2. Pin the checkout to that commit.
git -C ~/.mnemos checkout <SHA>

# 3. Re-run update with the network-pull step skipped so the pinned
#    commit is what lands in the install location.
mnemos update --skip-git-pull
# → re-syncs ~/.mnemos/core/ and ~/.mnemos/agents/ from the pinned tree,
#   re-templates managed blocks, re-runs migrate_policy_transient.
```

This sequence is idempotent (AC6), so re-running it is always safe.

### Memory-store rollback — restore captured data

For rolling back the memory store itself (the `wiki/` + `.agent/` trees,
not the host-config blocks), follow the operator-tested flow documented
in [`docs/backup-restore.md`](backup-restore.md). `mnemos backup` /
`mnemos restore` are the canonical tools; this guide deliberately does
NOT duplicate that procedure here.

---

## Related documentation

- [`docs/backup-restore.md`](backup-restore.md) — memory-store snapshots and
  point-in-time rollback (#75).
- [`docs/remote-sync.md`](remote-sync.md) — continuous git-backed sync
  (#79); the always-on companion to the explicit lifecycle commands.
- [`docs/release-workflow.md`](release-workflow.md) — the surrounding
  release cadence (#72 / #73) that produces the artifacts this guide
  installs.
- [`docs/security-privacy.md`](security-privacy.md) — privacy posture for
  the host blocks and memory store touched by these flows (#77).
