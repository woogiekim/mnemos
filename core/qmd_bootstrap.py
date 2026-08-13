"""Explicit QMD bootstrap support for Mnemos installs.

QMD is a derived retrieval index. This module only runs when the operator asks
for QMD during install/update; the default Mnemos install remains offline-safe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml


QMD_PACKAGE = "@tobilu/qmd"
_PACKAGE_MANAGERS = ("npm", "bun")

_QMD_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "executable": "qmd",
    "index_name": "mnemos",
    "mode": "search",
    "timeout_seconds": 15,
    "update_timeout_seconds": 120,
    "embed_on_update": False,
    "model_ready": False,
}


def bootstrap_qmd(repo_root: str | Path, *, package_manager: str = "auto") -> dict[str, Any]:
    """Install QMD when missing, enable model-free QMD config, and prepare it.

    The bootstrap is intentionally explicit because it may invoke a global
    package manager and network access. It never embeds models or runs semantic
    search preparation.
    """
    root = Path(repo_root).resolve()
    executable_before = shutil.which("qmd")
    manager = "none"
    installed = False

    if executable_before is None:
        manager = _resolve_package_manager(package_manager)
        _install_qmd(manager)
        installed = True
        executable = _find_qmd_executable(manager)
        if executable is None:
            raise RuntimeError("qmd install command completed but qmd executable was not found")
    else:
        executable = "qmd"

    _enable_qmd_config(root, executable=executable)

    from core.qmd_queue import _build_qmd_adapter

    adapter, collections = _build_qmd_adapter(root)
    config_path = adapter.prepare_index_config(collections)

    return {
        "installed": installed,
        "package_manager": manager,
        "prepared": True,
        "config_path": str(config_path),
        "collection_count": len(collections),
    }


def _resolve_package_manager(package_manager: str) -> str:
    requested = (package_manager or "auto").strip().lower()
    if requested != "auto":
        if requested not in _PACKAGE_MANAGERS:
            raise RuntimeError(f"unsupported QMD package manager: {package_manager}")
        if shutil.which(requested) is None:
            raise RuntimeError(f"{requested} is required to install QMD")
        return requested

    for candidate in _PACKAGE_MANAGERS:
        if shutil.which(candidate) is not None:
            return candidate

    raise RuntimeError("npm or bun is required to install QMD")


def _install_qmd(package_manager: str) -> None:
    if package_manager == "bun":
        command = ["bun", "install", "-g", QMD_PACKAGE]
    else:
        command = ["npm", "install", "-g", QMD_PACKAGE]

    subprocess.run(command, check=True)


def _find_qmd_executable(package_manager: str) -> str | None:
    existing = shutil.which("qmd")
    if existing:
        return "qmd"

    for candidate in _candidate_qmd_paths(package_manager):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return None


def _candidate_qmd_paths(package_manager: str) -> list[Path]:
    candidates: list[Path] = []
    if package_manager == "npm":
        result = subprocess.run(
            ["npm", "prefix", "-g"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            candidates.append(Path(result.stdout.strip()) / "bin" / "qmd")
    elif package_manager == "bun":
        bun_install = os.environ.get("BUN_INSTALL")
        if bun_install:
            candidates.append(Path(bun_install) / "bin" / "qmd")
        candidates.append(Path.home() / ".bun" / "bin" / "qmd")

    return candidates


def _enable_qmd_config(repo_root: Path, *, executable: str) -> None:
    config_path = repo_root / "mnemos.yml"
    config = _read_yaml_mapping(config_path)

    retrieval = config.get("retrieval")
    if not isinstance(retrieval, dict):
        retrieval = {}
    qmd = retrieval.get("qmd")
    if not isinstance(qmd, dict):
        qmd = {}

    merged = dict(_QMD_DEFAULTS)
    merged.update(qmd)
    merged["enabled"] = True
    merged["executable"] = executable
    merged["mode"] = str(merged.get("mode") or "search").strip().lower() or "search"
    if merged["mode"] in {"vsearch", "query"} and not merged.get("model_ready"):
        merged["model_ready"] = False

    retrieval["qmd"] = merged
    config["retrieval"] = retrieval

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    if not isinstance(loaded, dict):
        return {}
    return loaded
