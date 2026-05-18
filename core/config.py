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
To opt out, set ``sync.enabled: false`` explicitly.  The default backend
(MemoryStore) never grows sync wiring — see :class:`SyncConfig` for the
full field catalogue.

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
        false`` in ``mnemos.yml``.
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
class BackendConfig:
    """Resolved backend configuration."""

    #: Name of the selected backend: ``"default"`` or ``"obsidian"``.
    backend: str = "default"

    #: Absolute path to the Obsidian vault (``None`` when backend != obsidian).
    vault_path: str | None = None

    #: Sync configuration (always present; ``enabled=False`` by default).
    sync: SyncConfig = field(default_factory=SyncConfig)


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

    return BackendConfig(backend=backend_name, vault_path=vault_path, sync=sync)
