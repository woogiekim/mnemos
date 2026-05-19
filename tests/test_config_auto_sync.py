"""Tests for the auto-enable sync logic in core/config.py.

When storage.backend is obsidian and vault_path is configured,
sync.enabled must default to True without requiring explicit
sync.enabled: true in mnemos.yml.  An explicit sync.enabled: false
overrides the auto behavior.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_config_with_yaml(yaml_content: str, tmp_path: Path):
    """Return BackendConfig resolved from a temporary mnemos.yml."""
    from core.config import get_backend_config

    config_file = tmp_path / "mnemos.yml"
    config_file.write_text(yaml_content, encoding="utf-8")
    return get_backend_config(repo_root=str(tmp_path))


# ---------------------------------------------------------------------------
# Auto-enable: vault_path set → sync.enabled becomes True
# ---------------------------------------------------------------------------


class TestAutoEnableSync:
    """get_backend_config() must auto-enable sync when obsidian+vault_path."""

    def test_auto_enable_when_vault_path_set_no_sync_block(self, tmp_path):
        """No sync: block at all → auto-enable when vault_path configured."""
        vault = tmp_path / "vault"
        vault.mkdir()
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.backend == "obsidian"
        assert cfg.vault_path is not None
        assert cfg.sync.enabled is True, (
            "sync.enabled must be True when vault_path is set and no sync: block present"
        )

    def test_auto_enable_when_vault_path_set_sync_block_without_enabled(self, tmp_path):
        """sync: block present but no enabled key → auto-enable."""
        vault = tmp_path / "vault"
        vault.mkdir()
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
  sync:
    remote: origin
    branch: main
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.sync.enabled is True, (
            "sync.enabled must be True when sync: block has no explicit enabled key"
        )
        assert cfg.sync.remote == "origin"
        assert cfg.sync.branch == "main"

    def test_auto_enable_preserves_other_sync_fields(self, tmp_path):
        """Auto-enable must not disturb other sync config fields."""
        vault = tmp_path / "vault"
        vault.mkdir()
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
  sync:
    remote: backup
    branch: develop
    pull_rate_limit_seconds: 60
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.sync.enabled is True
        assert cfg.sync.remote == "backup"
        assert cfg.sync.branch == "develop"
        assert cfg.sync.pull_rate_limit_seconds == 60

    def test_explicit_true_preserved(self, tmp_path):
        """Explicit sync.enabled: true is preserved (no conflict with auto)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
  sync:
    enabled: true
    remote: origin
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.sync.enabled is True


# ---------------------------------------------------------------------------
# Explicit opt-out: sync.enabled: false must be respected
# ---------------------------------------------------------------------------


class TestExplicitOptOut:
    """Explicit sync.enabled: false must override the auto behavior."""

    def test_explicit_false_disables_sync(self, tmp_path):
        """sync.enabled: false explicitly → sync stays disabled."""
        vault = tmp_path / "vault"
        vault.mkdir()
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
  sync:
    enabled: false
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.backend == "obsidian"
        assert cfg.vault_path is not None
        assert cfg.sync.enabled is False, (
            "explicit sync.enabled: false must override auto-enable"
        )

    def test_explicit_false_string_variant(self, tmp_path):
        """YAML 'false' as a string literal is also respected."""
        vault = tmp_path / "vault"
        vault.mkdir()
        # Some YAML serialisers emit the string "false" rather than the bool
        yaml = f"""\
storage:
  backend: obsidian
  vault_path: {vault}
  sync:
    enabled: 'false'
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.sync.enabled is False


# ---------------------------------------------------------------------------
# No vault_path → auto-enable must NOT activate
# ---------------------------------------------------------------------------


class TestNoVaultPath:
    """Auto-enable must not fire when vault_path is absent."""

    def test_no_vault_path_no_auto_enable(self, tmp_path):
        """obsidian backend without vault_path → sync.enabled stays False."""
        yaml = """\
storage:
  backend: obsidian
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        # vault_path is None → auto-enable should not fire
        assert cfg.vault_path is None
        assert cfg.sync.enabled is False

    def test_default_backend_no_auto_enable(self, tmp_path):
        """Default backend (MemoryStore) → sync.enabled stays False."""
        yaml = """\
storage:
  backend: default
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.backend == "default"
        assert cfg.sync.enabled is False

    def test_no_storage_section_no_auto_enable(self, tmp_path):
        """Empty / no config → default backend → sync.enabled stays False."""
        yaml = """\
# no storage section
"""
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.backend == "default"
        assert cfg.sync.enabled is False


# ---------------------------------------------------------------------------
# MNEMOS_BACKEND env-var override interactions
# ---------------------------------------------------------------------------


class TestEnvVarInteractions:
    """MNEMOS_BACKEND env var interacts correctly with auto-enable."""

    def test_env_var_obsidian_with_vault_path_auto_enables(self, tmp_path):
        """MNEMOS_BACKEND=obsidian + vault_path in YAML → auto-enable fires."""
        from core.config import get_backend_config

        vault = tmp_path / "vault"
        vault.mkdir()
        config_file = tmp_path / "mnemos.yml"
        config_file.write_text(
            f"storage:\n  vault_path: {vault}\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"MNEMOS_BACKEND": "obsidian"}):
            cfg = get_backend_config(repo_root=str(tmp_path))
        assert cfg.backend == "obsidian"
        assert cfg.sync.enabled is True

    def test_env_var_obsidian_no_vault_path_no_auto_enable(self, tmp_path):
        """MNEMOS_BACKEND=obsidian but no vault_path → no auto-enable."""
        from core.config import get_backend_config

        config_file = tmp_path / "mnemos.yml"
        config_file.write_text("storage: {}\n", encoding="utf-8")
        with patch.dict("os.environ", {"MNEMOS_BACKEND": "obsidian"}):
            cfg = get_backend_config(repo_root=str(tmp_path))
        assert cfg.backend == "obsidian"
        assert cfg.vault_path is None
        assert cfg.sync.enabled is False


# ---------------------------------------------------------------------------
# SyncConfig defaults are unchanged (backward compatibility)
# ---------------------------------------------------------------------------


class TestSyncConfigDefaults:
    """SyncConfig dataclass defaults must remain backward-compatible."""

    def test_sync_config_default_enabled_is_false(self):
        """SyncConfig() default must still have enabled=False."""
        from core.config import SyncConfig
        cfg = SyncConfig()
        assert cfg.enabled is False, (
            "SyncConfig() default enabled must remain False for backward compat"
        )

    def test_parse_sync_config_empty_dict_returns_disabled(self):
        """_parse_sync_config({}) should return enabled=False."""
        from core.config import _parse_sync_config
        cfg = _parse_sync_config({})
        assert cfg.enabled is False

    def test_parse_sync_config_none_returns_disabled(self):
        """_parse_sync_config(None) should return enabled=False."""
        from core.config import _parse_sync_config
        cfg = _parse_sync_config(None)
        assert cfg.enabled is False
