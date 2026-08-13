"""Tests for mnemos.install."""
import yaml
from pathlib import Path

import pytest
from click.testing import CliRunner

from core.install import install, migrate_policy_transient, _WIKI_DIRS, _AGENT_DIRS, _GITIGNORE_BLOCK
from core.cli import cli


def test_install_creates_wiki_dirs(tmp_path):
    install(tmp_path)
    for rel in _WIKI_DIRS:
        assert (tmp_path / rel).is_dir(), f"Expected directory: {rel}"


def test_install_creates_agent_dirs(tmp_path):
    install(tmp_path)
    for rel in _AGENT_DIRS:
        assert (tmp_path / rel).is_dir(), f"Expected directory: {rel}"


def test_install_writes_mnemos_yml(tmp_path):
    install(tmp_path)
    config_path = tmp_path / "mnemos.yml"
    assert config_path.exists()
    config = yaml.safe_load(config_path.read_text())
    assert "layers" in config
    assert "ephemeral" in config["layers"]
    assert "global" in config["layers"]


def test_install_writes_policy_yaml(tmp_path):
    install(tmp_path)
    policy_path = tmp_path / "wiki" / "policy.yaml"
    assert policy_path.exists()
    policy = yaml.safe_load(policy_path.read_text())
    assert "layers" in policy
    assert set(policy["layers"].keys()) == {"transient", "ephemeral", "working", "session", "project", "global"}


def test_install_policy_transient_layer_config(tmp_path):
    """transient layer must have no promotes_to and a flat path template."""
    install(tmp_path)
    policy = yaml.safe_load((tmp_path / "wiki" / "policy.yaml").read_text())
    transient = policy["layers"]["transient"]
    assert transient["path_template"] == ".agent/transient/"
    assert transient["promotes_to"] is None


def test_install_creates_transient_dir(tmp_path):
    """install() must create .agent/transient/ alongside other agent dirs."""
    install(tmp_path)
    assert (tmp_path / ".agent" / "transient").is_dir()


def test_migrate_policy_transient_adds_layer(tmp_path):
    """migrate_policy_transient() inserts transient into a pre-existing policy."""
    # Scaffold a policy without transient (mirrors pre-fix installations).
    policy_path = tmp_path / "wiki" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    old_policy = {
        "layers": {
            "ephemeral": {"path_template": ".agent/runs/{run_id}/scratch/", "promotes_to": "working"},
            "global": {"path_template": "wiki/global/", "promotes_to": None},
        },
        "forget": {"requires_archived": True},
    }
    with policy_path.open("w") as f:
        yaml.dump(old_policy, f)

    changed = migrate_policy_transient(tmp_path)
    assert changed is True

    updated = yaml.safe_load(policy_path.read_text())
    assert "transient" in updated["layers"]
    assert updated["layers"]["transient"]["path_template"] == ".agent/transient/"
    assert updated["layers"]["transient"]["promotes_to"] is None
    # Original layers must be preserved
    assert "ephemeral" in updated["layers"]
    assert "global" in updated["layers"]
    # .agent/transient directory must be created
    assert (tmp_path / ".agent" / "transient").is_dir()


def test_migrate_policy_transient_idempotent(tmp_path):
    """migrate_policy_transient() returns False when transient already exists."""
    install(tmp_path)  # fresh install already includes transient
    changed = migrate_policy_transient(tmp_path)
    assert changed is False


def test_migrate_policy_transient_missing_file(tmp_path):
    """migrate_policy_transient() returns False gracefully when policy.yaml is absent."""
    changed = migrate_policy_transient(tmp_path)
    assert changed is False


def test_install_writes_agents_md(tmp_path):
    install(tmp_path)
    agents_path = tmp_path / "AGENTS.md"
    assert agents_path.exists()
    content = agents_path.read_text()
    assert "AGENTS.md" in content
    assert "mnemos" in content.lower()


def test_install_appends_gitignore(tmp_path):
    install(tmp_path)
    gitignore_path = tmp_path / ".gitignore"
    assert gitignore_path.exists()
    content = gitignore_path.read_text()
    assert "# mnemos" in content
    assert ".agent/runs/" in content


def test_install_appends_to_existing_gitignore(tmp_path):
    gitignore_path = tmp_path / ".gitignore"
    gitignore_path.write_text("*.pyc\n__pycache__/\n")
    install(tmp_path)
    content = gitignore_path.read_text()
    assert "*.pyc" in content
    assert "# mnemos" in content


def test_install_idempotent(tmp_path):
    install(tmp_path)
    install(tmp_path)

    config_path = tmp_path / "mnemos.yml"
    gitignore_path = tmp_path / ".gitignore"

    config = yaml.safe_load(config_path.read_text())
    assert "layers" in config

    gitignore_content = gitignore_path.read_text()
    assert gitignore_content.count("# mnemos") == 1


def test_install_does_not_overwrite_existing_mnemos_yml(tmp_path):
    config_path = tmp_path / "mnemos.yml"
    config_path.write_text("custom: true\n")
    install(tmp_path)
    assert config_path.read_text() == "custom: true\n"


def test_install_does_not_overwrite_existing_agents_md(tmp_path):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# My custom agents\n")
    install(tmp_path)
    assert agents_path.read_text() == "# My custom agents\n"


def test_install_cli_command(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["install", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "mnemos installed at" in result.output
    assert (tmp_path / "mnemos.yml").exists()


def test_install_cli_does_not_bootstrap_qmd_by_default(tmp_path, monkeypatch):
    called = False

    def fake_bootstrap(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("core.install.bootstrap_qmd", fake_bootstrap)

    runner = CliRunner()
    result = runner.invoke(cli, ["install", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert called is False


def test_install_cli_with_qmd_bootstraps_qmd(tmp_path, monkeypatch):
    calls = []

    def fake_bootstrap(repo_root, *, package_manager):
        calls.append((repo_root, package_manager))
        return {"installed": True, "prepared": True, "config_path": str(tmp_path / "qmd.yml")}

    monkeypatch.setattr("core.install.bootstrap_qmd", fake_bootstrap)

    runner = CliRunner()
    result = runner.invoke(cli, ["install", "--with-qmd", "--qmd-package-manager", "npm", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert calls == [(tmp_path.resolve(), "npm")]
    assert "qmd: installed=True prepared=True" in result.output


def test_install_cli_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["install"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "mnemos.yml").exists()


def test_install_writes_mnemos_owned_host_block(tmp_path):
    """Smoke test: detected Claude host receives mnemos-owned install assets."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("{}\n")
    (claude_dir / "CLAUDE.md").write_text("# Claude\n")

    install(tmp_path / "repo", home=home)

    claude_md = (claude_dir / "CLAUDE.md").read_text()
    assert "<!-- mnemos-start -->" in claude_md
    assert "mnemos search" in claude_md
    assert "mnemos read" in claude_md
    assert "mnemos gc" in claude_md
    assert "agent-crew-start" not in claude_md


def test_install_degrades_when_claude_hooks_unavailable(tmp_path):
    """Host install still writes memory instructions when hook settings are absent."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)

    install(tmp_path / "repo", home=home)

    claude_md = claude_dir / "CLAUDE.md"
    assert claude_md.exists()
    content = claude_md.read_text()
    assert "<!-- mnemos-start -->" in content
    assert "hooks are unavailable" in content
