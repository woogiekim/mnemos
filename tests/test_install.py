"""Tests for mnemos.install."""
import yaml
from pathlib import Path

import pytest
from click.testing import CliRunner

from mnemos.core.install import install, _WIKI_DIRS, _AGENT_DIRS, _GITIGNORE_BLOCK
from mnemos.core.cli import cli


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
    assert set(policy["layers"].keys()) == {"ephemeral", "working", "session", "project", "global"}


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


def test_install_cli_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["install"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "mnemos.yml").exists()
