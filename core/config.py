"""Configuration loader for mnemos.

Reads ``mnemos.yml`` from the repo root (or ``~/.mnemos.yml`` as fallback)
and exposes backend configuration to the gateway and CLI.

Priority order (highest → lowest):
1. ``MNEMOS_BACKEND`` environment variable (overrides everything)
2. ``mnemos.yml`` ``storage.backend`` / ``storage.vault_path`` fields
3. Defaults (backend=``"default"``, vault_path=``None``)

Example ``mnemos.yml``::

    storage:
      backend: obsidian
      vault_path: ~/Documents/ObsidianVault
      sync:
        enabled: false          # opt-out override — omitting this key auto-enables sync
        remote: origin
        branch: main
        mode: auto              # auto | manual
        auto_pull_on_capture: true
        auto_push_after_commit: true
        commit_message_template: "mnemos: {layer} {id} ({timestamp})"
        pull_rate_limit_seconds: 30

Auto-sync: when ``storage.backend: obsidian`` and ``vault_path`` is set,
sync is **enabled automatically** without requiring ``sync.enabled: true``.
To opt out, set ``sync.enabled: false`` explicitly.

The default backend (MemoryStore) also supports optional remote git-sync as of
issue #69, but it is **opt-in only**: sync activates for the default backend
exclusively when ``storage.sync.enabled: true`` is set explicitly.  The
auto-enable rule below never fires for the default backend, so a default-backend
project never commits/pushes unless the user asks for it.  See
:class:`SyncConfig` for the full field catalogue.

When no git remote is configured in the vault, git push/pull operations are
skipped silently so the backend works in local-only mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import]
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False


#: Default commit message template — supports {layer}, {id}, {timestamp}.
DEFAULT_COMMIT_MESSAGE_TEMPLATE = "mnemos: {layer} {id} ({timestamp})"

#: Number of seconds to skip a pull when the last successful pull is recent.
DEFAULT_PULL_RATE_LIMIT_SECONDS = 30


@dataclass
class SyncConfig:
    """Resolved ``storage.sync.*`` configuration.

    All fields have safe defaults so callers can always read them without
    checking whether the ``sync:`` block was present in the YAML file.

    .. note::

        The :attr:`enabled` field is ``False`` by default here.  The
        :func:`get_backend_config` function overrides it to ``True``
        automatically when ``backend == "obsidian"`` and a ``vault_path``
        is configured — unless the user explicitly set ``sync.enabled:
        false`` in ``mnemos.yml``.  For the **default** backend (issue #69)
        the auto-enable rule never fires: sync is enabled only when the user
        explicitly sets ``sync.enabled: true``.
    """

    #: Master switch.  When ``False`` (the default) no sync hooks run and
    #: the ObsidianBackend behaves exactly as before #24.  Callers should
    #: not set this field directly — use :func:`get_backend_config` which
    #: applies the auto-enable rule for the Obsidian backend.
    enabled: bool = False

    #: Remote name for pull/push (default: ``"origin"``).
    remote: str = "origin"

    #: Branch to pull from / push to (default: ``"main"``).
    branch: str = "main"

    #: ``"auto"`` (hooks fire automatically) or ``"manual"`` (user calls
    #: ``mnemos sync pull/push`` explicitly).
    mode: str = "auto"

    #: When ``True`` and ``mode == "auto"``, a pull is attempted before each
    #: write if the rate-limit window has expired.
    auto_pull_on_capture: bool = True

    #: When ``True`` and a commit was actually created, push immediately.
    auto_push_after_commit: bool = True

    #: Python :meth:`str.format` template for commit messages.
    #: Supported placeholders: ``{layer}``, ``{id}``, ``{timestamp}``.
    commit_message_template: str = DEFAULT_COMMIT_MESSAGE_TEMPLATE

    #: Minimum number of seconds between automatic pulls.  A pull is skipped
    #: when the last successful pull occurred within this window.
    pull_rate_limit_seconds: int = DEFAULT_PULL_RATE_LIMIT_SECONDS


@dataclass
class DistillationConfig:
    """Resolved ``storage.distillation.*`` configuration (issue #87).

    Controls the automatic distillation pipeline that subscribes to the
    ``post-capture`` event seam and runs the domain + policy distill helpers
    when the captures-since-last-distill counter crosses
    :attr:`interval_captures`. Both fields have tolerant fall-back to
    defaults when the ``mnemos.yml`` block is missing or invalid, so existing
    projects pick up the feature automatically on upgrade.
    """

    #: Master switch.  When ``True`` (the default) the gateway registers a
    #: post-capture subscriber and also fires distill at the end of
    #: :meth:`MemoryGateway.consolidate`.  Set to ``False`` in
    #: ``mnemos.yml`` to opt out entirely.
    enabled: bool = True

    #: Number of captures between automatic distill fires.  Must be a
    #: positive integer; invalid (non-int, ``<= 0``) or missing values
    #: fall back to the default of 25.
    interval_captures: int = 25


@dataclass
class LiveUpdateConfig:
    """Resolved ``app.live_update.*`` configuration (issue #95).

    Controls the long-running desktop-app file watcher that pushes
    re-rendered payloads into the open pywebview window when the memory
    store changes. Tolerant of missing / malformed input — a non-bool
    ``enabled`` falls back to ``True``; a non-int / non-positive
    ``debounce_ms`` falls back to ``300``. Mirrors
    :class:`DistillationConfig` so both blocks behave identically when
    ``mnemos.yml`` is empty or invalid.
    """

    #: Master switch.  When ``True`` (the default) the app starts a
    #: file-system watcher that debounces events into a JS bridge call.
    #: Set ``app.live_update.enabled: false`` in ``mnemos.yml`` to opt out
    #: entirely; the window is then a static snapshot as before #95.
    enabled: bool = True

    #: Debounce window in milliseconds.  Events arriving within this
    #: window after the first event are coalesced into a single rebuild.
    #: Must be a positive integer; invalid (non-int, ``<= 0``) or missing
    #: values fall back to the default of 300.
    debounce_ms: int = 300


@dataclass
class AppConfig:
    """Resolved ``app.*`` configuration (issue #95).

    Top-level container for desktop-app-specific knobs.  Every nested
    block has safe defaults so callers may read them without checking
    whether the ``app:`` block was present in the YAML file.
    """

    #: Live-update configuration (always present; ``enabled=True`` by
    #: default — issue #95).
    live_update: LiveUpdateConfig = field(default_factory=LiveUpdateConfig)


@dataclass
class BackendConfig:
    """Resolved backend configuration."""

    #: Name of the selected backend: ``"default"`` or ``"obsidian"``.
    backend: str = "default"

    #: Absolute path to the Obsidian vault (``None`` when backend != obsidian).
    vault_path: str | None = None

    #: Sync configuration (always present; ``enabled=False`` by default).
    sync: SyncConfig = field(default_factory=SyncConfig)

    #: Auto-distillation configuration (always present; ``enabled=True`` by
    #: default — issue #87).
    distillation: DistillationConfig = field(default_factory=DistillationConfig)

    #: Application-level configuration block (issue #95). Always populated
    #: with the dataclass defaults; carries the live-update knobs the
    #: desktop app reads on launch.
    app: AppConfig = field(default_factory=AppConfig)


def _load_yaml_config(repo_root: str | None = None) -> dict[str, Any]:
    """Try to load ``mnemos.yml`` from *repo_root* or ``~/.mnemos.yml``."""
    if not _HAS_YAML:
        return {}

    candidates: list[Path] = []
    if repo_root:
        candidates.append(Path(repo_root) / "mnemos.yml")
    candidates.append(Path.home() / ".mnemos.yml")

    for path in candidates:
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = _yaml.safe_load(f)
                    return data if isinstance(data, dict) else {}
            except Exception:
                pass
    return {}


def _parse_sync_config(sync_raw: Any) -> SyncConfig:
    """Parse the ``storage.sync`` sub-dict into a :class:`SyncConfig`.

    Missing keys fall back to the dataclass defaults.  Unknown keys are
    silently ignored so future schema additions remain forward-compatible.
    """
    if not isinstance(sync_raw, dict):
        return SyncConfig()

    def _bool(v: Any, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("true", "1", "yes")
        return default

    def _int(v: Any, default: int) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    return SyncConfig(
        enabled=_bool(sync_raw.get("enabled"), False),
        remote=str(sync_raw.get("remote", "origin")),
        branch=str(sync_raw.get("branch", "main")),
        mode=str(sync_raw.get("mode", "auto")),
        auto_pull_on_capture=_bool(sync_raw.get("auto_pull_on_capture"), True),
        auto_push_after_commit=_bool(sync_raw.get("auto_push_after_commit"), True),
        commit_message_template=str(
            sync_raw.get("commit_message_template", DEFAULT_COMMIT_MESSAGE_TEMPLATE)
        ),
        pull_rate_limit_seconds=_int(
            sync_raw.get("pull_rate_limit_seconds"), DEFAULT_PULL_RATE_LIMIT_SECONDS
        ),
    )


def _parse_distillation_config(raw: Any) -> DistillationConfig:
    """Parse the ``storage.distillation`` sub-dict into :class:`DistillationConfig`.

    Tolerant of every kind of malformed input: a non-dict ``raw`` (None,
    string, list, …) yields the defaults; a non-bool ``enabled`` falls back
    to ``True``; a non-int / non-positive ``interval_captures`` falls back to
    ``25``. Missing keys fall through to the dataclass defaults. This mirrors
    :func:`_parse_sync_config` so the behaviour around invalid YAML is
    identical between the two config blocks.
    """
    if not isinstance(raw, dict):
        return DistillationConfig()

    enabled_raw = raw.get("enabled", True)
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = True

    interval_raw = raw.get("interval_captures", 25)
    if isinstance(interval_raw, bool):
        # ``bool`` is a subclass of ``int`` in Python; treat True/False as
        # invalid here so the user's intent ("how many captures") is not
        # silently coerced to 1 or 0.
        interval = 25
    elif isinstance(interval_raw, int) and interval_raw > 0:
        interval = interval_raw
    else:
        interval = 25

    return DistillationConfig(enabled=enabled, interval_captures=interval)


def _parse_live_update_config(raw: Any) -> LiveUpdateConfig:
    """Parse the ``app.live_update`` sub-dict into :class:`LiveUpdateConfig`.

    Tolerant of every kind of malformed input (issue #95): a non-dict
    ``raw`` (None, string, list, …) yields the defaults; a non-bool
    ``enabled`` falls back to ``True``; a non-int / non-positive
    ``debounce_ms`` falls back to ``300``. Missing keys fall through to
    the dataclass defaults. This mirrors :func:`_parse_distillation_config`
    so the behaviour around invalid YAML is identical between blocks.
    """
    if not isinstance(raw, dict):
        return LiveUpdateConfig()

    enabled_raw = raw.get("enabled", True)
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = True

    debounce_raw = raw.get("debounce_ms", 300)
    if isinstance(debounce_raw, bool):
        # ``bool`` is a subclass of ``int``; treat True/False as invalid
        # so the user's intent ("how many ms") is not silently coerced
        # to 1 or 0.
        debounce = 300
    elif isinstance(debounce_raw, int) and debounce_raw > 0:
        debounce = debounce_raw
    else:
        debounce = 300

    return LiveUpdateConfig(enabled=enabled, debounce_ms=debounce)


def get_backend_config(repo_root: str | None = None) -> BackendConfig:
    """Return the resolved :class:`BackendConfig` for the current environment.

    :param repo_root: Path to the mnemos repo root.  When ``None``, only
        the environment variable and ``~/.mnemos.yml`` are consulted.
    """
    # ── 1. Environment variable override ──────────────────────────────────
    env_backend = os.environ.get("MNEMOS_BACKEND", "").strip().lower()

    # ── 2. YAML config ────────────────────────────────────────────────────
    config = _load_yaml_config(repo_root)
    storage_cfg = config.get("storage") or {}
    yaml_backend = str(storage_cfg.get("backend", "")).strip().lower()
    yaml_vault_path = storage_cfg.get("vault_path")

    # Resolve the effective backend name
    backend_name = env_backend or yaml_backend or "default"

    # Resolve vault path (only relevant for obsidian backend)
    vault_path: str | None = None
    if backend_name == "obsidian":
        raw_vp = yaml_vault_path
        if raw_vp:
            vault_path = str(Path(str(raw_vp)).expanduser().resolve())

    # Resolve sync config (only meaningful for obsidian; always populated)
    sync_raw = storage_cfg.get("sync")
    sync = _parse_sync_config(sync_raw)

    # ── Auto-enable sync for Obsidian when vault_path is set ──────────────
    # When backend == "obsidian" and vault_path is configured, sync is
    # activated automatically — the user does not need to write
    # ``sync.enabled: true``.  The only way to opt out is to explicitly
    # set ``sync.enabled: false`` in the YAML.
    #
    # Logic: if the sync sub-block is absent OR the ``enabled`` key is not
    # present in it, treat the absence as "auto" (activate).  Only a
    # literal ``enabled: false`` suppresses auto-activation.
    if backend_name == "obsidian" and vault_path:
        sync_raw_dict = sync_raw if isinstance(sync_raw, dict) else {}
        if "enabled" not in sync_raw_dict:
            # No explicit opinion from the user — auto-enable
            sync.enabled = True
        # If "enabled" IS in sync_raw_dict, _parse_sync_config() already
        # applied the user's explicit value (True or False) — do not override.

    # Resolve auto-distillation config (issue #87). Always populated; defaults
    # are ``enabled=True`` and ``interval_captures=25`` so existing projects
    # pick up the feature automatically on upgrade. Set
    # ``storage.distillation.enabled: false`` in ``mnemos.yml`` to opt out.
    distillation_raw = storage_cfg.get("distillation")
    distillation = _parse_distillation_config(distillation_raw)

    # Resolve the top-level ``app`` block (issue #95). Always populated with
    # defaults; the only nested key for now is ``live_update``. Set
    # ``app.live_update.enabled: false`` in ``mnemos.yml`` to opt out of the
    # desktop app's live-update file watcher.
    app_raw = config.get("app") or {}
    live_update_raw = app_raw.get("live_update") if isinstance(app_raw, dict) else None
    live_update = _parse_live_update_config(live_update_raw)
    app_cfg = AppConfig(live_update=live_update)

    return BackendConfig(
        backend=backend_name,
        vault_path=vault_path,
        sync=sync,
        distillation=distillation,
        app=app_cfg,
    )
