"""Tests for the live-update config block (issue #95).

Covers :class:`core.config.LiveUpdateConfig`, :class:`core.config.AppConfig`,
:func:`core.config._parse_live_update_config`, and the
:func:`core.config.get_backend_config` integration. Mirrors the shape of
``tests/test_auto_distill.py``'s config helpers — same defaults / opt-out
/ invalid-input matrix.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_config_with_yaml(yaml_content: str, tmp_path: Path):
    from core.config import get_backend_config

    config_file = tmp_path / "mnemos.yml"
    config_file.write_text(yaml_content, encoding="utf-8")
    return get_backend_config(repo_root=str(tmp_path))


# ---------------------------------------------------------------------------
# Defaults — no app block at all
# ---------------------------------------------------------------------------
class TestLiveUpdateDefaults:
    """When ``mnemos.yml`` carries no ``app:`` block, defaults apply."""

    def test_default_enabled_true(self, tmp_path):
        cfg = _get_config_with_yaml("storage:\n  backend: default\n", tmp_path)
        assert cfg.app.live_update.enabled is True

    def test_default_debounce_ms_300(self, tmp_path):
        cfg = _get_config_with_yaml("storage:\n  backend: default\n", tmp_path)
        assert cfg.app.live_update.debounce_ms == 300

    def test_no_yaml_at_all_defaults(self, tmp_path):
        """No mnemos.yml → defaults still apply (the loader returns an empty dict)."""
        from core.config import get_backend_config

        cfg = get_backend_config(repo_root=str(tmp_path))
        assert cfg.app.live_update.enabled is True
        assert cfg.app.live_update.debounce_ms == 300


# ---------------------------------------------------------------------------
# Opt-out — app.live_update.enabled: false
# ---------------------------------------------------------------------------
class TestLiveUpdateOptOut:
    def test_explicit_false_disables(self, tmp_path):
        yaml = "app:\n  live_update:\n    enabled: false\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is False

    def test_explicit_true_enables(self, tmp_path):
        yaml = "app:\n  live_update:\n    enabled: true\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is True


# ---------------------------------------------------------------------------
# Custom debounce_ms — positive int accepted
# ---------------------------------------------------------------------------
class TestLiveUpdateDebounce:
    def test_custom_debounce_ms_accepted(self, tmp_path):
        yaml = "app:\n  live_update:\n    debounce_ms: 500\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 500

    def test_custom_debounce_ms_one_is_accepted(self, tmp_path):
        yaml = "app:\n  live_update:\n    debounce_ms: 1\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 1


# ---------------------------------------------------------------------------
# Invalid input — fall back to safe defaults
# ---------------------------------------------------------------------------
class TestLiveUpdateInvalidInput:
    def test_non_bool_enabled_falls_back_to_true(self, tmp_path):
        """A string ``enabled`` is invalid — falls back to True."""
        yaml = "app:\n  live_update:\n    enabled: \"maybe\"\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is True

    def test_int_enabled_falls_back_to_true(self, tmp_path):
        """An int ``enabled`` is invalid — falls back to True."""
        yaml = "app:\n  live_update:\n    enabled: 7\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is True

    def test_zero_debounce_falls_back_to_300(self, tmp_path):
        yaml = "app:\n  live_update:\n    debounce_ms: 0\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 300

    def test_negative_debounce_falls_back_to_300(self, tmp_path):
        yaml = "app:\n  live_update:\n    debounce_ms: -1\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 300

    def test_string_debounce_falls_back_to_300(self, tmp_path):
        yaml = "app:\n  live_update:\n    debounce_ms: \"slow\"\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 300

    def test_bool_debounce_falls_back_to_300(self, tmp_path):
        """``bool`` is a subclass of ``int`` — True/False are invalid here."""
        yaml = "app:\n  live_update:\n    debounce_ms: true\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.debounce_ms == 300

    def test_non_dict_app_block_yields_defaults(self, tmp_path):
        """``app`` set to a scalar → defaults still apply."""
        yaml = "app: \"not a dict\"\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is True
        assert cfg.app.live_update.debounce_ms == 300

    def test_non_dict_live_update_block_yields_defaults(self, tmp_path):
        yaml = "app:\n  live_update: \"bogus\"\n"
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.app.live_update.enabled is True
        assert cfg.app.live_update.debounce_ms == 300


# ---------------------------------------------------------------------------
# Direct parser helper — full surface coverage
# ---------------------------------------------------------------------------
class TestParseLiveUpdateConfigDirect:
    def test_parser_with_none_returns_defaults(self):
        from core.config import LiveUpdateConfig, _parse_live_update_config

        cfg = _parse_live_update_config(None)
        assert isinstance(cfg, LiveUpdateConfig)
        assert cfg.enabled is True
        assert cfg.debounce_ms == 300

    def test_parser_with_list_returns_defaults(self):
        from core.config import _parse_live_update_config

        cfg = _parse_live_update_config([1, 2, 3])
        assert cfg.enabled is True
        assert cfg.debounce_ms == 300

    def test_parser_with_partial_dict_uses_defaults_for_missing(self):
        from core.config import _parse_live_update_config

        cfg = _parse_live_update_config({"enabled": False})
        assert cfg.enabled is False
        assert cfg.debounce_ms == 300

    def test_parser_with_partial_dict_debounce_only(self):
        from core.config import _parse_live_update_config

        cfg = _parse_live_update_config({"debounce_ms": 1000})
        assert cfg.enabled is True
        assert cfg.debounce_ms == 1000


# ---------------------------------------------------------------------------
# Coexistence — live-update config does not disturb other blocks
# ---------------------------------------------------------------------------
class TestLiveUpdateCoexistence:
    def test_other_blocks_unaffected(self, tmp_path):
        """Setting app.live_update doesn't perturb storage / sync / distillation."""
        yaml = (
            "storage:\n  backend: default\n"
            "  distillation:\n    enabled: false\n    interval_captures: 99\n"
            "app:\n  live_update:\n    enabled: false\n    debounce_ms: 750\n"
        )
        cfg = _get_config_with_yaml(yaml, tmp_path)
        assert cfg.backend == "default"
        assert cfg.distillation.enabled is False
        assert cfg.distillation.interval_captures == 99
        assert cfg.app.live_update.enabled is False
        assert cfg.app.live_update.debounce_ms == 750
