# mnemos Desktop App — `mnemos.app` (macOS)

> Operator guide for [issue #94](https://github.com/woogiekim/mnemos/issues/94)
> — packaging the unified inspection UI (#83/#86/#90/#91/#92/#93) as an
> installable macOS `.app` bundle via PyInstaller. The bundle ships the Python
> runtime, pywebview, and the mnemos backend so it does not depend on a
> pre-installed `mnemos` CLI.

> Scope: deliver one working macOS `.app` bundle. The `mnemos ui` CLI continues
> unchanged — the desktop app is an ALTERNATIVE entry point, not a replacement.
> The PyInstaller spec is written to be cross-platform-friendly so future
> issues just need to run pyinstaller on Linux/Windows to extend the surface.

> Companion docs — cross-linked, not duplicated:
>
> - [Unified Inspection UI](./unified-inspection-ui.md) — the
>   [#83](https://github.com/woogiekim/mnemos/issues/83) HTML surface this app
>   wraps. The desktop app does NOT change the UI itself; it merely hosts the
>   same payload in a native window.
> - [Install / Update / Rollback Lifecycle](./install-lifecycle.md) — the
>   [#74](https://github.com/woogiekim/mnemos/issues/74) operator contract for
>   the `~/.mnemos` install root the `.app` reads from by default.

---

## TL;DR

```bash
# 1. Install the build extra into your active venv.
pip install 'mnemos[ui,build]'

# 2. Build the macOS .app bundle (unsigned, local-only).
bash scripts/build_app.sh --execute

# 3. Open the assembled bundle.
open dist/mnemos.app
```

The first launch will be blocked by macOS Gatekeeper (the bundle is unsigned).
**Right-click → Open** to bypass it once; subsequent launches double-click
normally.

---

## Build prerequisites

The build dep is opt-in via a new `[project.optional-dependencies]` group:

```toml
[project.optional-dependencies]
ui = ["pywebview>=5.0"]
build = ["pyinstaller>=6.0"]
```

So a fresh build needs BOTH extras:

```bash
pip install 'mnemos[ui,build]'
```

`pywebview` is the runtime UI dep (already required by `mnemos ui` from #83).
`pyinstaller` is a BUILD-TIME dep only — it is NEVER added to the base
`[project] dependencies`, so end users running `pip install mnemos` do not
pull it.

You should use a real Python virtualenv (`python3 -m venv .venv` →
`source .venv/bin/activate`). PyInstaller refuses to bundle into a Homebrew
Python because the system framework Python's signature is not relocatable.

---

## Build commands

Two entry points produce the same artifact:

```bash
# Helper: dry-run by default; --execute invokes pyinstaller and verifies.
bash scripts/build_app.sh --execute

# Or call pyinstaller directly (the helper just wraps this).
pyinstaller --noconfirm app/mnemos_app.spec
```

`scripts/build_app.sh` is intentionally dry-run-by-default: with no flags (or
with `--dry-run`) it prints the planned command without invoking pyinstaller.
With `--execute` it invokes pyinstaller, then verifies the assembled bundle
carries `Contents/Info.plist`, `Contents/MacOS/mnemos`, and
`Contents/Resources/core/templates/ui.html` — the load-bearing files. The
helper NEVER codesigns, notarizes, or pushes; those are deliberate, separate,
human steps.

The build writes to `dist/` and `build/` only. Both are local-only artifacts
and are not part of the repo's tracked surface.

---

## `.app` location

After `--execute` completes:

```text
dist/mnemos.app/
├── Contents/
│   ├── Info.plist                    # CFBundleIdentifier="io.mnemos.app"
│   ├── MacOS/
│   │   └── mnemos                    # the entry binary
│   ├── Resources/
│   │   └── core/templates/
│   │       ├── ui.html               # unified UI surface (#83)
│   │       ├── graph.html
│   │       ├── inspect.html
│   │       └── __init__.py
│   └── Frameworks/                   # bundled Python + pywebview
└── (mnemos.app is the user-facing artifact — drag into /Applications)
```

`scripts/build_app.sh --execute` prints the final `.app` path and its size
(`du -sh`) on completion.

---

## macOS Gatekeeper first-launch bypass

The built bundle is **unsigned**. macOS Gatekeeper will block the first launch
with `"mnemos.app" cannot be opened because the developer cannot be verified.`
or — depending on macOS version — quarantine the binary outright.

**Bypass it once, manually:**

1. In Finder, navigate to `dist/mnemos.app` (or wherever you dragged it).
2. Right-click (or Control-click) the bundle → **Open**.
3. macOS shows a second confirmation dialog with an Open button. Click it.
4. Subsequent launches double-click normally — Gatekeeper remembers the
   exception.

mnemos deliberately does NOT attempt to bypass Gatekeeper programmatically
(no `spctl --add`, no `xattr -d com.apple.quarantine` in the build script).
Codesigning + notarization is a separate, deliberate, human workflow.

---

## `MNEMOS_REPO_ROOT` precedence

The bundled binary resolves its memory store via the same env var the CLI
uses, with a single safe default:

1. **`MNEMOS_REPO_ROOT` env var** — if set in the launching environment, the
   app honors it verbatim.
2. **`~/.mnemos` default** — if the env var is unset (the common case for a
   Finder-launched `.app`), the entry sets it to `os.path.expanduser("~/.mnemos")`
   BEFORE importing `core.cli`, so the gateway bootstraps against the
   standard install root that `install.sh` provisions.

> **Important:** Finder-launched apps do NOT inherit your shell's exported
> env vars. If you have `export MNEMOS_REPO_ROOT=...` in `~/.zshrc` to point
> the CLI at a non-default location, the `.app` will NOT see it when opened
> from the Dock or `open` command. To point the `.app` at a non-default
> store, launch it from a terminal with the env var on the command line:
>
> ```bash
> MNEMOS_REPO_ROOT=/path/to/store open dist/mnemos.app
> ```
>
> Or, for a one-shot headless render:
>
> ```bash
> MNEMOS_REPO_ROOT=/path/to/store \
>   MNEMOS_APP_HEADLESS=1 \
>   dist/mnemos.app/Contents/MacOS/mnemos
> ```

---

## How the `.app` differs from the CLI

| Aspect | `mnemos ui` (CLI) | `mnemos.app` (desktop) |
|---|---|---|
| Entry | `core.cli:memory_ui` (Click command) | `app/mnemos_app.py:main()` |
| Backend | `_get_gateway` + `unifiedview` | **same** — reused verbatim |
| Payload contract | `build_unified_payload` | **same** — no new logic |
| HTML rendering | `render_html` + `write_unified_html` | **same** functions |
| Window | `launch_app` (pywebview file://) | **same** launcher |
| Repo-root default | `.` (current dir) | `~/.mnemos` (Finder-safe) |
| Headless render | `mnemos ui --output PATH` | `MNEMOS_APP_HEADLESS=1` |
| Live-data | refresh on next CLI invocation | **YES** — file-watcher pushes re-renders via the JS bridge (#95) |

The desktop app is presentation only — the same payload builders, same
templates, same launcher. The only difference is the entry point and the
defaulted repo root.

---

## Headless mode

The bundled binary honors a single env switch for CI / smoke-test paths:

```bash
MNEMOS_APP_HEADLESS=1 \
  MNEMOS_APP_HEADLESS_OUTPUT=/tmp/mnemos-app-headless.html \
  dist/mnemos.app/Contents/MacOS/mnemos
```

When `MNEMOS_APP_HEADLESS=1`, the entry writes the unified HTML via
`write_unified_html` to `MNEMOS_APP_HEADLESS_OUTPUT` (default
`/tmp/mnemos-app-headless.html`) and `sys.exit(0)`. The headless path NEVER
imports `webview`, so it works on a machine without a window server (a CI
runner, a remote ssh session, an unsigned bundle that has not yet been
Gatekeeper-bypassed).

This is the path the opt-in build-smoke test (`pytest -m app_build`) uses to
prove the assembled `.app` actually runs.

---

## Branded icon — drop in your `.icns`

The default spec ships without an icon (`icon=None` on the `BUNDLE()` and
`EXE()` steps); macOS uses its generic application icon. To brand the bundle:

1. Produce a `.icns` file from your 1024x1024 source (e.g. via `iconutil
   --convert icns mnemos.iconset`).
2. Drop it next to the spec: `app/mnemos.icns`.
3. Uncomment the single line in `app/mnemos_app.spec`:

   ```python
   # icon="mnemos.icns",
   ```

   becomes

   ```python
   icon="mnemos.icns",
   ```

4. Rebuild: `bash scripts/build_app.sh --execute`.

The icon ships inside `Contents/Resources/mnemos.icns` automatically.

---

## Cross-platform extensibility (follow-ups)

The PyInstaller spec is structured so Linux and Windows targets can extend it
without re-architecting:

* **Linux** — drop the `BUNDLE()` step (Linux ships the `COLLECT()` directory
  or an AppImage built from it) and extend `hiddenimports` with
  `webview.platforms.gtk`. The mnemos backend modules port unchanged.
* **Windows** — replace the `BUNDLE()` step with a Windows `.exe` from
  `COLLECT()`, extend `hiddenimports` with `webview.platforms.winforms` (or
  `qt`), and supply a `.ico` via `EXE(icon=...)`. The backend modules port
  unchanged.

Both are tracked as follow-up issues; the macOS `.app` ships in #94 as the
first target.

---

## Live updates

> Issue [#95](https://github.com/woogiekim/mnemos/issues/95) — the desktop app
> rebuilds its UI in real time as the memory store changes, so a `mnemos
> capture` from another shell appears in the open window within a few hundred
> milliseconds without a manual reload.

### How it works

The app starts a file-system watcher in the background **after**
`create_window` returns but **before** `webview.start()` enters its blocking
event loop. The watcher subscribes to the store root the gateway reads from:

* **default `MemoryStore`** → `<repo_root>/wiki` (recursive)
* **`ObsidianBackend`** → `<vault_path>` (recursive)

When a file event fires, a single `threading.Timer` is armed (or re-armed) for
the configured debounce window. After the quiet window elapses, the watcher:

1. Re-walks every layer through `gateway._store.iter_layer_items(...)`.
2. Calls `build_unified_payload(...)` against the same policy engine the
   initial render used — no new payload logic.
3. Serialises the new payload with `json.dumps(...)` and pipes it through
   `window.evaluate_js("window.mnemos.applyUpdate(<json>)")`.

The JS side (`window.mnemos.applyUpdate(payload)`) snapshots the live UI
state, mutates the module-scoped arrays in place, re-renders, and restores
the snapshot. Preserved state:

| Surface | Preserved by |
|---|---|
| Active tab | `document.querySelector('button[role="tab"][aria-selected="true"]').id` |
| Drilldown selection | `window.__currentDrilldownId` |
| Sidebar domain row | `.domain-row[aria-selected="true"]` `data-domain-row` |
| Search input | `#search-input.value` |
| Graph node positions | `Map<nodeId, {x, y, _pinned}>` |
| Expand toggles | `li[data-mem-id]` carrying `.mem-content-full.shown` |
| Result-list scroll | `#result-list.scrollTop` |

Concurrent pushes are serialised — a follow-up event arriving while a rebuild
is still in flight sets a "follow-up" flag and the in-flight rebuild re-arms
the timer when it completes, so the latest payload always wins without
overlapping JS bridge calls. Rebuild exceptions are caught and logged via
`core.observability` (when importable) — a transient store error never
crashes the long-running app.

### Config knobs

The `mnemos.yml` `app.live_update` block controls the feature. Defaults
match the issue spec; both keys are tolerant of malformed input (a non-bool
`enabled` falls back to `true`; a non-int / non-positive `debounce_ms` falls
back to `300`).

```yaml
app:
  live_update:
    enabled: true          # default true; set false to opt out
    debounce_ms: 300       # default 300; positive int
```

### Opt-out

To run the app as a static snapshot (the pre-#95 behavior), set
`enabled: false`:

```yaml
app:
  live_update:
    enabled: false
```

The watcher does not start and `atexit` does not register a shutdown hook —
the GUI path becomes identical to #94.

### Manual smoke test

From the built `.app` (or `app/mnemos_app.py` directly) in one terminal:

```bash
open dist/mnemos.app
```

From a second terminal, capture a memory and watch it appear in the Memory
tab within ~`debounce_ms` ms:

```bash
mnemos capture "live-update probe — $(date)"
```

The new memory should appear in the Memory tab without losing the currently
active tab, sidebar selection, search query, or expanded rows.

### Troubleshooting

* **Window does not update** — verify the resolved store root with
  `mnemos search "" --limit 1` from the same shell that launches the app.
  Finder-launched apps do NOT inherit shell env, so `MNEMOS_REPO_ROOT` must
  be set on the command line (`MNEMOS_REPO_ROOT=/path open dist/mnemos.app`)
  if it's not the default `~/.mnemos`.
* **Updates are bursty/too aggressive** — raise `app.live_update.debounce_ms`
  to coalesce more events into a single rebuild.
* **Updates feel laggy** — lower `app.live_update.debounce_ms`. The smallest
  sensible value is ~50–100ms; tighter windows degrade into per-event JS
  bridge thrash.
* **`[ui]` extra missing** — install with `pip install 'mnemos[ui]'`. The
  watcher depends on `watchdog>=4.0`, which is now part of the `[ui]` extra.
* **Memory store on a network filesystem** — `watchdog`'s `Observer` falls
  back to polling on filesystems that do not emit kernel events; expect a
  larger effective latency on those mounts. Local APFS/HFS+/ext4 stores
  receive events synchronously.
