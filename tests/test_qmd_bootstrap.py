"""Tests for explicit QMD install/update bootstrap."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from core import qmd_bootstrap


class _FakeQmdAdapter:
    def __init__(self, config_path: Path):
        self.config_path = config_path

    def prepare_index_config(self, collections):
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text("index: mnemos\n", encoding="utf-8")
        return self.config_path


def test_bootstrap_qmd_uses_existing_binary_and_enables_model_free_config(tmp_path, monkeypatch):
    """When qmd is already on PATH, bootstrap must not run a package manager."""
    config_path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        qmd_bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected install")),
    )
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda repo_root: (_FakeQmdAdapter(config_path), {"wiki": tmp_path / "wiki"}),
    )

    result = qmd_bootstrap.bootstrap_qmd(tmp_path)

    assert result["installed"] is False
    assert result["package_manager"] == "none"
    assert result["prepared"] is True

    config = yaml.safe_load((tmp_path / "mnemos.yml").read_text(encoding="utf-8"))
    qmd = config["retrieval"]["qmd"]
    assert qmd["enabled"] is True
    assert qmd["mode"] == "search"
    assert qmd["embed_on_update"] is False
    assert qmd["model_ready"] is False


def test_bootstrap_qmd_installs_missing_binary_with_auto_npm(tmp_path, monkeypatch):
    """Auto mode must choose an available package manager only when qmd is missing."""
    config_path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
    calls: list[list[str]] = []
    qmd_checks = iter([None, "/usr/local/bin/qmd"])

    def fake_which(name: str):
        if name == "qmd":
            return next(qmd_checks)
        if name == "npm":
            return "/usr/local/bin/npm"
        return None

    def fake_run(command, check=False):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", fake_which)
    monkeypatch.setattr(qmd_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda repo_root: (_FakeQmdAdapter(config_path), {}),
    )

    result = qmd_bootstrap.bootstrap_qmd(tmp_path)

    assert result["installed"] is True
    assert result["package_manager"] == "npm"
    assert calls == [["npm", "install", "-g", "@tobilu/qmd"]]
    config = yaml.safe_load((tmp_path / "mnemos.yml").read_text(encoding="utf-8"))
    assert config["retrieval"]["qmd"]["executable"] == "qmd"


def test_bootstrap_qmd_records_global_bin_when_new_qmd_is_not_on_path(tmp_path, monkeypatch):
    """A global install can succeed before the current shell PATH includes the bin dir."""
    config_path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
    global_qmd = tmp_path / "npm-prefix" / "bin" / "qmd"
    global_qmd.parent.mkdir(parents=True)
    global_qmd.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    global_qmd.chmod(0o755)

    def fake_which(name: str):
        if name == "npm":
            return "/usr/local/bin/npm"
        return None

    def fake_run(command, check=False, capture_output=False, text=False):
        if command == ["npm", "prefix", "-g"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(tmp_path / "npm-prefix"))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", fake_which)
    monkeypatch.setattr(qmd_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda repo_root: (_FakeQmdAdapter(config_path), {}),
    )

    result = qmd_bootstrap.bootstrap_qmd(tmp_path, package_manager="npm")

    assert result["installed"] is True
    config = yaml.safe_load((tmp_path / "mnemos.yml").read_text(encoding="utf-8"))
    assert config["retrieval"]["qmd"]["executable"] == str(global_qmd)


def test_bootstrap_qmd_uses_explicit_bun(tmp_path, monkeypatch):
    config_path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
    calls: list[list[str]] = []
    qmd_checks = iter([None, "/opt/bin/qmd"])

    def fake_which(name: str):
        if name == "qmd":
            return next(qmd_checks)
        if name == "bun":
            return "/opt/bin/bun"
        return None

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", fake_which)
    monkeypatch.setattr(qmd_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda repo_root: (_FakeQmdAdapter(config_path), {}),
    )

    result = qmd_bootstrap.bootstrap_qmd(tmp_path, package_manager="bun")

    assert result["package_manager"] == "bun"
    assert calls == [["bun", "install", "-g", "@tobilu/qmd"]]


def test_bootstrap_qmd_fails_when_requested_manager_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(qmd_bootstrap.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="bun is required"):
        qmd_bootstrap.bootstrap_qmd(tmp_path, package_manager="bun")


def test_bootstrap_qmd_rejects_unknown_package_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(qmd_bootstrap.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="unsupported QMD package manager"):
        qmd_bootstrap.bootstrap_qmd(tmp_path, package_manager="pnpm")


def test_bootstrap_qmd_fails_when_auto_has_no_package_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(qmd_bootstrap.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="npm or bun is required"):
        qmd_bootstrap.bootstrap_qmd(tmp_path)


def test_bootstrap_qmd_fails_when_install_does_not_expose_qmd(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_which(name: str):
        if name == "npm":
            return "/usr/local/bin/npm"
        return None

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", fake_which)
    monkeypatch.setattr(qmd_bootstrap.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="executable was not found"):
        qmd_bootstrap.bootstrap_qmd(tmp_path, package_manager="npm")

    assert calls == [
        ["npm", "install", "-g", "@tobilu/qmd"],
        ["npm", "prefix", "-g"],
    ]


def test_candidate_qmd_paths_uses_bun_install_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "bun-home"))
    monkeypatch.setattr(qmd_bootstrap.Path, "home", staticmethod(lambda: tmp_path / "home"))

    candidates = qmd_bootstrap._candidate_qmd_paths("bun")

    assert candidates == [
        tmp_path / "bun-home" / "bin" / "qmd",
        tmp_path / "home" / ".bun" / "bin" / "qmd",
    ]


def test_read_yaml_mapping_tolerates_scalar_config(tmp_path):
    config_path = tmp_path / "mnemos.yml"
    config_path.write_text("- not\n- mapping\n", encoding="utf-8")

    assert qmd_bootstrap._read_yaml_mapping(config_path) == {}


def test_bootstrap_qmd_preserves_existing_semantic_settings(tmp_path, monkeypatch):
    config_path = tmp_path / ".agent" / "state" / "qmd" / "config" / "mnemos.yml"
    (tmp_path / "mnemos.yml").write_text(
        yaml.dump(
            {
                "retrieval": {
                    "qmd": {
                        "enabled": False,
                        "mode": "vsearch",
                        "embed_model": "hf:example/model.gguf",
                        "model_ready": False,
                    }
                }
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(qmd_bootstrap.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        "core.qmd_queue._build_qmd_adapter",
        lambda repo_root: (_FakeQmdAdapter(config_path), {}),
    )

    qmd_bootstrap.bootstrap_qmd(tmp_path)

    config = yaml.safe_load((tmp_path / "mnemos.yml").read_text(encoding="utf-8"))
    qmd = config["retrieval"]["qmd"]
    assert qmd["enabled"] is True
    assert qmd["mode"] == "vsearch"
    assert qmd["embed_model"] == "hf:example/model.gguf"
    assert qmd["model_ready"] is False
