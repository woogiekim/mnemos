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

Multi-host sync is out of scope for this PR.  The vault path is whatever
the user configures; iCloud / git-based sync is the user's responsibility.
See the README for guidance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml  # type: ignore[import]
    _HAS_YAML = True
except ImportError:  # pragma: no cover
    _HAS_YAML = False


@dataclass
class BackendConfig:
    """Resolved backend configuration."""

    #: Name of the selected backend: ``"default"`` or ``"obsidian"``.
    backend: str = "default"

    #: Absolute path to the Obsidian vault (``None`` when backend != obsidian).
    vault_path: str | None = None


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

    return BackendConfig(backend=backend_name, vault_path=vault_path)
