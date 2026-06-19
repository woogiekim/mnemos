"""Codex host adapter tests."""
from __future__ import annotations

from pathlib import Path


def _make_codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "AGENTS.md").write_text("# Existing Codex rules\n", encoding="utf-8")
    return home


def test_codex_adapter_installs_memory_block_in_agents_md(tmp_path: Path) -> None:
    """Codex should receive the same proactive Mnemos behavior rules as other hosts."""
    from core.adapters.codex import CODEX_AGENTS_BLOCK, CodexAdapter

    home = _make_codex_home(tmp_path)
    messages = CodexAdapter().install(home)

    agents_md = home / ".codex" / "AGENTS.md"
    content = agents_md.read_text(encoding="utf-8")
    assert "# Existing Codex rules" in content
    assert CODEX_AGENTS_BLOCK in content
    assert "mnemos search" in content
    assert "proactively" in content
    assert any("[installed]" in message for message in messages)


def test_codex_adapter_is_idempotent_and_verifiable(tmp_path: Path) -> None:
    """Repeated installs should keep one managed block and verify_hooks should pass."""
    from core.adapters.codex import CodexAdapter

    home = _make_codex_home(tmp_path)
    adapter = CodexAdapter()
    adapter.install(home)
    adapter.install(home)

    content = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert content.count("<!-- mnemos:start -->") == 1
    assert content.count("<!-- mnemos:end -->") == 1
    assert adapter.verify_hooks(home) == (True, [])


def test_codex_adapter_uninstall_removes_only_managed_block(tmp_path: Path) -> None:
    """Uninstall should preserve user-owned Codex AGENTS.md content."""
    from core.adapters.codex import CodexAdapter

    home = _make_codex_home(tmp_path)
    adapter = CodexAdapter()
    adapter.install(home)
    messages = adapter.uninstall(home)

    content = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
    assert "# Existing Codex rules" in content
    assert "<!-- mnemos:start -->" not in content
    assert "<!-- mnemos:end -->" not in content
    assert any("[removed]" in message for message in messages)

