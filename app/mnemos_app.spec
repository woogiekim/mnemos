# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the mnemos desktop app (issue #94).
#
# Build:
#     pyinstaller --noconfirm app/mnemos_app.spec
#
# Or via the helper:
#     bash scripts/build_app.sh --execute
#
# Output:
#     dist/mnemos.app                                       (macOS bundle)
#     dist/mnemos.app/Contents/MacOS/mnemos                  (entry binary)
#     dist/mnemos.app/Contents/Resources/core/templates/*.html (UI templates)
#
# -----------------------------------------------------------------------------
# Cross-platform notes — follow-up issues will extend this spec for Linux and
# Windows. The current scope is macOS .app (issue #94). To add another OS:
#
#   * Linux (PyInstaller --onedir + AppImage wrapper):
#       extend hiddenimports with `webview.platforms.gtk` (and one of
#       `gi.repository.Gtk`, `gi.repository.WebKit2`); drop the BUNDLE() step
#       below — Linux ships the COLLECT() directory or an AppImage built from
#       it. Test on the target distro because GTK runtime deps are dynamic.
#
#   * Windows (PyInstaller --onedir + NSIS/Inno installer):
#       extend hiddenimports with `webview.platforms.winforms` (or `qt` if the
#       user prefers Qt — both ship in pywebview). Replace BUNDLE() with a
#       Windows `.exe` produced by COLLECT(); add a `.ico` via the EXE(icon=)
#       argument. The mnemos backend modules listed below port without change.
# -----------------------------------------------------------------------------

block_cipher = None


a = Analysis(
    ["mnemos_app.py"],
    pathex=["."],
    binaries=[],
    datas=[
        # The unified UI ships three HTML templates that the bundled binary
        # must be able to read at runtime via Path(...).read_text(). They live
        # under Contents/Resources/core/templates/ in the assembled .app.
        ("../core/templates/ui.html", "core/templates"),
        ("../core/templates/graph.html", "core/templates"),
        ("../core/templates/inspect.html", "core/templates"),
        ("../core/templates/__init__.py", "core/templates"),
    ],
    hiddenimports=[
        # pywebview platform module — Cocoa on macOS. PyInstaller cannot see
        # this through the lazy `import webview` in core.unifiedview.launch_app
        # so we declare it explicitly. (For Linux/Windows follow-ups see the
        # header comment.)
        "webview",
        "webview.platforms.cocoa",
        "objc",
        "Foundation",
        "WebKit",
        "AppKit",
        # mnemos backend modules — most are reachable via the entry's static
        # imports, but a few are loaded lazily inside core/cli.py and would be
        # missed by PyInstaller's import scanner without an explicit hint.
        "core.unifiedview",
        "core.graphview",
        "core.inspectview",
        "core.cohesion",
        "core.gateway",
        "core.policy",
        "core.store",
        "core.layers",
        "core.cli",
        "core.templates",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Strip the obvious heavy-but-unused stdlib + third-party modules to
        # keep the .app under control. The unified UI is pure HTML + tiny
        # JSON; none of these are reachable.
        "tkinter",
        "test",
        "unittest",
        "pydoc",
        "numpy",
        "pandas",
        "scipy",
        "sklearn",
        "matplotlib",
        "PIL",
        "IPython",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mnemos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # To ship a branded icon, drop ``app/mnemos.icns`` next to this spec and
    # uncomment the line below. The spec stays valid without an icon — macOS
    # uses the generic application icon.
    # icon="mnemos.icns",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="mnemos",
)

app = BUNDLE(
    coll,
    name="mnemos.app",
    icon=None,
    bundle_identifier="io.mnemos.app",
    info_plist={
        "CFBundleName": "mnemos",
        "CFBundleDisplayName": "mnemos",
        "CFBundleIdentifier": "io.mnemos.app",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)
