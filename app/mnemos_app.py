"""mnemos desktop app entry — thin pywebview launcher (issue #94).

This is the **alternative** entry point for the unified inspection UI delivered
in #83. The CLI (``mnemos ui``) remains the primary path; this module is what
the PyInstaller-built ``mnemos.app`` bundle's ``Contents/MacOS/mnemos`` binary
executes on launch.

Responsibilities (in execution order):

1. **Resolve ``MNEMOS_REPO_ROOT`` BEFORE any ``core`` import.** A Finder-launched
   ``.app`` does NOT inherit the shell environment, so the env var is usually
   absent at launch. We fall back to ``~/.mnemos`` (the standard install root
   that ``install.sh`` provisions) so the bundled app agrees with the CLI by
   default. The resolution MUST happen before ``import core.cli`` because the
   gateway helper reads the env var during module load when called.
2. **Build the unified payload** by reusing :func:`core.cli._get_gateway`,
   :data:`core.layers.LAYER_STATIC_PATHS`, :func:`core.unifiedview.build_unified_payload`
   verbatim — no new payload logic is introduced. We mirror the layer-walk
   sequence at ``core/cli.py:3155-3168`` so the app sees exactly the same
   memory set the CLI does.
3. **Branch on the ``MNEMOS_APP_HEADLESS`` env flag.**
   * ``MNEMOS_APP_HEADLESS == "1"`` → call :func:`core.unifiedview.write_unified_html`
     to write the HTML to ``MNEMOS_APP_HEADLESS_OUTPUT`` (default
     ``/tmp/mnemos-app-headless.html``), then ``sys.exit(0)``. Any exception
     prints to stderr and exits 1. This branch is what the build-smoke test
     and CI invoke; it never imports ``webview``.
   * Otherwise (the default GUI path) → :func:`core.unifiedview.render_html`
     then :func:`core.unifiedview.launch_app` with ``title="mnemos"``. The
     ``[ui]`` extra (pywebview) is required on this branch; the bundled
     ``.app`` ships pywebview inside ``Contents/MacOS``.

The module is intentionally short — every payload / rendering decision lives
in ``core/`` and is therefore covered by the existing 100% gate on the core
surface. This file lives OUTSIDE ``core/`` + ``agents/`` so it is NOT part of
the coverage gate but IS subject to ``filterwarnings=["error"]``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    """Entry point for ``mnemos.app`` — runs at process start."""
    # Step 1: resolve the repo root BEFORE importing core.cli. Finder-launched
    # apps do not inherit shell env, so the default ~/.mnemos must apply when
    # the env var is unset.
    if not os.environ.get("MNEMOS_REPO_ROOT"):
        os.environ["MNEMOS_REPO_ROOT"] = os.path.expanduser("~/.mnemos")

    # Step 2: import the reused builders. The ``core.cli`` import is what
    # triggers gateway/policy bootstrap; doing it AFTER the env var is set
    # ensures the gateway sees the correct root.
    from core.cli import _get_gateway
    from core.layers import LAYER_STATIC_PATHS
    from core.unifiedview import (
        build_unified_payload,
        launch_app,
        render_html,
        write_unified_html,
    )

    gw = _get_gateway()

    # Mirror core/cli.py:3155-3168 layer-walk verbatim — same layer order, same
    # iteration shape — so the desktop app and ``mnemos ui`` always agree on
    # which items appear.
    static_layers = list(LAYER_STATIC_PATHS.keys())
    dynamic_layers = ["ephemeral", "working", "session"]
    all_layers = static_layers + dynamic_layers

    items: list[dict] = []
    for layer in all_layers:
        for item in gw._store.iter_layer_items(layer):
            items.append(item)

    # Step 3a: headless path — render to a file and exit 0. Used by the
    # build-smoke test and CI. NEVER imports webview.
    if os.environ.get("MNEMOS_APP_HEADLESS") == "1":
        out = os.environ.get(
            "MNEMOS_APP_HEADLESS_OUTPUT", "/tmp/mnemos-app-headless.html"
        )
        try:
            write_unified_html(items, Path(out), gw._policy)
        except Exception as exc:  # pragma: no cover - defensive guard
            print(f"mnemos.app headless error: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    # Step 3b: GUI path — build the payload, render HTML, and host it in a
    # native pywebview window via the #83 file:// launcher.
    payload = build_unified_payload(items, gw._policy)
    html = render_html(payload)

    # Step 3c (#95): live updates. When ``app.live_update.enabled`` is True
    # (the default), start a file watcher that re-renders the payload and
    # pushes it through the JS bridge whenever the memory store changes.
    # The watcher is wired AFTER ``create_window`` returns (via the
    # ``ready`` callback) so we capture the actual window object, then
    # stopped on process exit via ``atexit``. When the user opts out via
    # ``app.live_update.enabled: false`` the original static call-site
    # behavior is preserved verbatim.
    from core.config import get_backend_config

    cfg = get_backend_config(repo_root=os.environ["MNEMOS_REPO_ROOT"])
    if cfg.app.live_update.enabled:
        import atexit
        import json as _json

        from app.live_watcher import LiveWatcher, resolve_watched_paths

        def on_ready(window: object) -> None:
            def rebuild() -> None:
                try:
                    live_items: list[dict] = []
                    for layer in all_layers:
                        live_items.extend(gw._store.iter_layer_items(layer))
                    new_payload = build_unified_payload(live_items, gw._policy)
                    js = (
                        "window.mnemos && window.mnemos.applyUpdate("
                        + _json.dumps(new_payload)
                        + ");"
                    )
                    window.evaluate_js(js)  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover - defensive guard
                    # Never let a rebuild error crash the long-running app.
                    pass

            watcher = LiveWatcher(
                paths=resolve_watched_paths(gw),
                debounce_ms=cfg.app.live_update.debounce_ms,
                on_rebuild=rebuild,
            )
            watcher.start()
            atexit.register(watcher.stop)

        launch_app(html, title="mnemos", ready=on_ready)
    else:
        launch_app(html, title="mnemos")


if __name__ == "__main__":  # pragma: no cover - executed only by the bundled binary
    main()
