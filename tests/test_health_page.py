"""Tests for ObsidianBackend._health.md generation.

Tests: content structure, wikilink format, regeneration idempotency.
TDD: written before the implementation.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backend(tmp_path: Path, vault_subdir: str = "vault"):
    from core.fts import FTSIndex
    from core.obsidian import ObsidianBackend
    vault_path = tmp_path / vault_subdir
    vault_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / ".agent" / "state" / "fts.db")
    fts = FTSIndex(db_path=db_path)
    return ObsidianBackend(vault_path=str(vault_path), fts=fts)


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------


class TestHealthPageStructure:
    def test_health_page_created_at_vault_root(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        assert (tmp_path / "vault" / "_health.md").exists()

    def test_health_page_has_title(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        assert "# mnemos Health" in content

    def test_health_page_has_all_sections(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        required_sections = [
            "## Orphans",
            "## Stale Items",
            "## Low Quality",
        ]
        for section in required_sections:
            assert section in content, f"Missing section: {section}"

    def test_health_page_has_generation_timestamp(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        assert "_Generated:" in content

    def test_health_page_has_sync_warning(self, tmp_path):
        """Health page must note that multi-host sync is out of scope."""
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        # Should mention sync limitation
        lower = content.lower()
        assert "sync" in lower or "multi-host" in lower or "icloud" in lower


# ---------------------------------------------------------------------------
# Wikilink format
# ---------------------------------------------------------------------------


class TestHealthPageWikilinks:
    def test_low_quality_items_use_wikilinks(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="lq-wiki-001",
            content="low quality item",
            metadata={"id": "lq-wiki-001", "layer": "project", "quality_score": 0.05},
        )
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        assert "[[lq-wiki-001]]" in content

    def test_normal_quality_items_not_in_low_quality_section(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="global",
            item_id="hq-wiki-001",
            content="high quality item",
            metadata={"id": "hq-wiki-001", "layer": "global", "quality_score": 0.95},
        )
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        # Should not appear in Low Quality section
        lines = content.splitlines()
        in_low_quality = False
        for line in lines:
            if "## Low Quality" in line:
                in_low_quality = True
            elif line.startswith("## "):
                in_low_quality = False
            elif in_low_quality and "hq-wiki-001" in line:
                pytest.fail("High-quality item should not appear in Low Quality section")

    def test_wikilinks_use_double_bracket_syntax(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="session",
            item_id="bracket-test-001",
            content="bracket test",
            metadata={"id": "bracket-test-001", "layer": "session", "quality_score": 0.1},
        )
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        # Must use [[id]] not [id](...) markdown link syntax
        assert "[[bracket-test-001]]" in content
        assert "[bracket-test-001](" not in content


# ---------------------------------------------------------------------------
# Regeneration idempotency
# ---------------------------------------------------------------------------


class TestHealthPageIdempotency:
    def test_regeneration_produces_same_sections(self, tmp_path):
        backend = _make_backend(tmp_path)
        backend.write(
            layer="project",
            item_id="idem-001",
            content="idempotency test",
            metadata={"id": "idem-001", "layer": "project", "quality_score": 0.1},
        )
        backend.generate_health_page()
        first = (tmp_path / "vault" / "_health.md").read_text()

        backend.generate_health_page()
        second = (tmp_path / "vault" / "_health.md").read_text()

        def strip_ts(text: str) -> str:
            return "\n".join(
                line for line in text.splitlines()
                if not line.startswith("_Generated:")
            )

        assert strip_ts(first) == strip_ts(second), (
            "Health page regeneration must be idempotent (same vault state → same output)"
        )

    def test_regeneration_overwrites_previous_file(self, tmp_path):
        """generate_health_page overwrites the file, not appends."""
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        first_size = (tmp_path / "vault" / "_health.md").stat().st_size
        backend.generate_health_page()
        second_size = (tmp_path / "vault" / "_health.md").stat().st_size
        # If it appended, file would grow; it should stay the same size
        # Allow small variation due to timestamp length differences (±20 bytes)
        assert abs(first_size - second_size) < 50, (
            "Health page must be overwritten on each regeneration, not appended"
        )

    def test_multiple_regenerations_stable(self, tmp_path):
        """Three consecutive regenerations produce identical content."""
        backend = _make_backend(tmp_path)
        results = []
        for _ in range(3):
            backend.generate_health_page()
            content = (tmp_path / "vault" / "_health.md").read_text()
            lines = [
                line for line in content.splitlines()
                if not line.startswith("_Generated:")
            ]
            results.append("\n".join(lines))
        assert results[0] == results[1] == results[2]


# ---------------------------------------------------------------------------
# Empty vault
# ---------------------------------------------------------------------------


class TestHealthPageEmptyVault:
    def test_empty_vault_generates_health_page(self, tmp_path):
        """Health page generates without error even with no items."""
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        assert (tmp_path / "vault" / "_health.md").exists()

    def test_empty_vault_sections_say_none(self, tmp_path):
        """All sections report 'None' or similar when vault is empty."""
        backend = _make_backend(tmp_path)
        backend.generate_health_page()
        content = (tmp_path / "vault" / "_health.md").read_text()
        lower = content.lower()
        # Each section should contain 'none', 'no items', or be otherwise empty-safe
        assert "none" in lower or "no items" in lower or "clean" in lower
