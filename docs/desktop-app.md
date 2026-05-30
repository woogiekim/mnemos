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
| Live-data | refresh on next CLI invocation | **NO** live data yet — that is the
                                          scope of follow-up
                                          [#95](https://github.com/woogiekim/mnemos/issues/95) |

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
