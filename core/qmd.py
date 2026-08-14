"""Optional local QMD retrieval and derived-index adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from core.config import QmdConfig, _qmd_index_name


_SEARCH_COMMANDS = {"query", "vsearch", "search"}
_MODEL_BACKED_SEARCH_COMMANDS = {"query", "vsearch"}
_DEFAULT_COLLECTION_IGNORE = ("**/domain-*.md",)


def _typed_vector_query(query: str) -> str:
    """Build a single typed QMD vector query without invoking expansion."""
    one_line = " ".join(line.strip() for line in query.splitlines()).strip()

    return f"vec: {one_line}"


def _qmd_embed_is_active(index_name: str, config_dir: Path) -> bool:
    """Return True when a foreground QMD search would compete with embedding."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    if completed.returncode != 0:
        return False

    candidate_pids: list[str] = []
    for line in completed.stdout.splitlines():
        command_text = f" {line} "
        if not ("/qmd " in command_text or " qmd " in command_text or "qmd.js " in command_text):
            continue
        if _qmd_embed_command_matches(line, index_name):
            pid = line.strip().split(maxsplit=1)[0]
            if pid.isdigit():
                candidate_pids.append(pid)

    return any(_qmd_process_uses_config_dir(pid, config_dir) for pid in candidate_pids)


def _qmd_embed_command_matches(command: str, index_name: str) -> bool:
    needle = f"--index {index_name} embed"
    alternate = f"--index={index_name} embed"

    return needle in command or alternate in command


def _qmd_process_uses_config_dir(pid: str, config_dir: Path) -> bool:
    try:
        completed = subprocess.run(
            ["ps", "eww", "-p", pid, "-o", "command="],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    if completed.returncode != 0:
        return False

    config_marker = f"QMD_CONFIG_DIR={config_dir}"
    for line in completed.stdout.splitlines():
        if config_marker in line:
            return True

    return False


class QmdCommandError(RuntimeError):
    """Content-free QMD process failure suitable for durable retry metadata."""

    def __init__(self, operation: str, code: str) -> None:
        self.operation = operation
        self.code = code
        super().__init__(f"qmd {operation} failed: {code}")


class QmdAdapter:
    """Invoke QMD through argv/JSON while keeping canonical memory in mnemos."""

    def __init__(
        self,
        *,
        repo_root: str | Path,
        store: Any,
        config: QmdConfig,
    ) -> None:
        self._root = Path(repo_root).expanduser().resolve()
        self._store = store
        self._config = config
        self._state_root = self._root / ".agent" / "state" / "qmd"
        self._config_dir = self._state_root / "config"
        self._cache_dir = self._state_root / "cache"
        self._index_name = _qmd_index_name(config.index_name, "mnemos")
        self._index_config_path = self._config_dir / f"{self._index_name}.yml"
        self._collection_roots: dict[str, Path] = {}
        self._last_diagnostics = self._readiness_diagnostics()

    def prepare_index_config(self, collections: dict[str, str | Path]) -> Path:
        """Write the repo-local QMD index definition on a mutation/setup path."""
        self._config_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        resolved_collections: dict[str, dict[str, Any]] = {}
        self._collection_roots = {}
        for name, raw_path in sorted(collections.items()):
            path = Path(raw_path).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            self._collection_roots[name] = path
            resolved_collections[name] = {
                "path": str(path),
                "pattern": "**/*.md",
                "ignore": list(_DEFAULT_COLLECTION_IGNORE),
                "includeByDefault": True,
            }

        payload: dict[str, Any] = {"collections": resolved_collections}
        if self._config.embed_model:
            payload["models"] = {"embed": self._config.embed_model}
        tmp_path = self._index_config_path.with_suffix(".yml.tmp")
        tmp_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=True),
            encoding="utf-8",
        )
        tmp_path.replace(self._index_config_path)
        self._last_diagnostics = self._readiness_diagnostics()

        return self._index_config_path

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Return canonical mnemos items for QMD JSON search results."""
        if not self._config.enabled:
            self._last_diagnostics = self._diagnostics_payload(
                status="disabled",
                available=False,
                degraded=False,
                reason="qmd backend is disabled",
                result_count=0,
            )
            return []
        if self._config.mode in {"vsearch", "query"} and not self._config.model_ready:
            self._last_diagnostics = self._diagnostics_payload(
                status="model_not_ready",
                available=False,
                degraded=True,
                reason="model-backed qmd search requires explicit readiness",
                result_count=0,
            )
            return []
        if (
            self._config.mode in _MODEL_BACKED_SEARCH_COMMANDS
            and _qmd_embed_is_active(self._index_name, self._config_dir)
        ):
            self._last_diagnostics = self._diagnostics_payload(
                status="busy",
                available=True,
                degraded=True,
                reason="qmd embedding is active",
                result_count=0,
            )
            return []
        if not self._index_config_path.is_file():
            self._last_diagnostics = self._diagnostics_payload(
                status="not_configured",
                available=False,
                degraded=True,
                reason="repo-local qmd index config is missing",
                result_count=0,
            )
            return []

        executable = self._resolve_executable()
        if executable is None:
            self._last_diagnostics = self._diagnostics_payload(
                status="missing",
                available=False,
                degraded=True,
                reason="qmd executable was not found",
                result_count=0,
            )
            return []

        command = self._config.mode if self._config.mode in _SEARCH_COMMANDS else "search"
        qmd_query = query
        extra_args: list[str] = []
        if command == "vsearch":
            command = "query"
            qmd_query = _typed_vector_query(query)
            extra_args.append("--no-rerank")
        argv = [
            executable,
            "--index",
            self._index_name,
            command,
            *extra_args,
            "--format",
            "json",
            "--full-path",
            "-n",
            str(max(1, int(limit))),
            qmd_query,
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self._root),
                env=self._subprocess_env(),
                capture_output=True,
                text=True,
                timeout=max(0.01, float(self._config.timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._last_diagnostics = self._diagnostics_payload(
                status="timeout",
                available=False,
                degraded=True,
                reason="qmd search exceeded its configured timeout",
                result_count=0,
            )
            return []
        except OSError as exc:
            self._last_diagnostics = self._diagnostics_payload(
                status="error",
                available=False,
                degraded=True,
                reason=exc.__class__.__name__,
                result_count=0,
            )
            return []

        if completed.returncode != 0:
            self._last_diagnostics = self._diagnostics_payload(
                status="error",
                available=False,
                degraded=True,
                reason=f"qmd exited with code {completed.returncode}",
                result_count=0,
            )
            return []

        try:
            raw_results = json.loads(completed.stdout)
            if isinstance(raw_results, dict):
                raw_results = raw_results.get("results")
            if not isinstance(raw_results, list):
                raise TypeError("qmd output must be a JSON array")
        except (json.JSONDecodeError, TypeError):
            self._last_diagnostics = self._diagnostics_payload(
                status="invalid_output",
                available=True,
                degraded=True,
                reason="qmd returned invalid JSON output",
                result_count=0,
            )
            return []

        results: list[dict[str, Any]] = []
        for raw_result in raw_results:
            mapped = self._map_result(raw_result)
            if mapped is not None:
                results.append(mapped)

        refresh = self._refresh_queue_state()
        stale = any(refresh.values())
        self._last_diagnostics = self._diagnostics_payload(
            status="stale" if stale else "available",
            available=True,
            degraded=stale,
            reason="qmd refresh queue is not converged" if stale else None,
            result_count=len(results),
        )
        self._last_diagnostics.update(
            {
                "refresh_pending": refresh["pending"],
                "refresh_processing": refresh["processing"],
                "refresh_failed": refresh["failed"],
            }
        )
        return results

    def update_index(
        self,
        collections: dict[str, str | Path],
    ) -> dict[str, bool]:
        """Refresh the derived QMD index and optionally its embeddings."""
        if not self._config.enabled:
            raise QmdCommandError("update", "disabled")

        self.prepare_index_config(collections)
        self._run_index_command("update")
        embedded = False
        if self._config.embed_on_update:
            if not self._config.model_ready:
                raise QmdCommandError("embed", "model_not_ready")
            self._run_index_command("embed")
            embedded = True

        return {"updated": True, "embedded": embedded}

    def diagnostics(self) -> dict[str, Any]:
        return dict(self._last_diagnostics)

    def _run_index_command(self, operation: str) -> None:
        executable = self._resolve_executable()
        if executable is None:
            raise QmdCommandError(operation, "missing_executable")

        try:
            completed = subprocess.run(
                [
                    executable,
                    "--index",
                    self._index_name,
                    operation,
                ],
                cwd=str(self._root),
                env=self._subprocess_env(),
                capture_output=True,
                text=True,
                timeout=max(0.01, float(self._config.update_timeout_seconds)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise QmdCommandError(operation, "timeout") from exc
        except OSError as exc:
            raise QmdCommandError(operation, "process_error") from exc

        if completed.returncode != 0:
            raise QmdCommandError(operation, "nonzero_exit")

    def _resolve_executable(self) -> str | None:
        configured = self._config.executable.strip()
        candidate = Path(configured).expanduser()
        separators = {separator for separator in (os.sep, os.altsep, "/", "\\") if separator}
        has_path_separator = any(separator in configured for separator in separators)
        if not candidate.is_absolute() and not has_path_separator:
            return shutil.which(configured)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["QMD_CONFIG_DIR"] = str(self._config_dir)
        env["XDG_CACHE_HOME"] = str(self._cache_dir)
        if self._config.embed_model:
            env["QMD_EMBED_MODEL"] = self._config.embed_model
        return env

    def _map_result(self, raw_result: Any) -> dict[str, Any] | None:
        if not isinstance(raw_result, dict):
            return None
        raw_file = raw_result.get("file") or raw_result.get("path")
        if not isinstance(raw_file, str) or not raw_file:
            return None
        path = self._resolve_result_path(raw_file)
        if path is None:
            return None

        try:
            item = dict(self._store.parse_file(path))
        except Exception:
            return None
        item_id = str(item.get("id") or path.stem)
        content = str(item.get("content") or "")
        try:
            raw_score = max(0.0, float(raw_result.get("score", 0.0)))
        except (TypeError, ValueError):
            raw_score = 0.0
        effective_mode = (
            self._config.mode if self._config.mode in _SEARCH_COMMANDS else "search"
        )
        if effective_mode == "search":
            score = raw_score / (1.0 + raw_score)
        else:
            score = min(1.0, raw_score)

        metadata = {
            key: value
            for key, value in item.items()
            if key not in {"content", "_path"}
        }
        metadata.update(
            {
                "qmd_docid": raw_result.get("docid"),
                "qmd_file": raw_file,
                "qmd_line": raw_result.get("line"),
            }
        )
        return {
            "item_id": item_id,
            "content": content,
            "metadata": metadata,
            "score": score,
            "semantic_score": score,
            "source": "qmd",
        }

    def _resolve_result_path(self, raw_file: str) -> Path | None:
        if raw_file.startswith("qmd://"):
            remainder = raw_file[len("qmd://"):]
            collection_name, separator, relative = remainder.partition("/")
            root = self._collections().get(collection_name)
            if not separator or root is None:
                return None
            path = (root / relative).resolve()
        else:
            candidate = Path(raw_file).expanduser()
            path = candidate.resolve() if candidate.is_absolute() else (self._root / candidate).resolve()

        allowed_roots = self._collections().values()
        if not any(path == root or path.is_relative_to(root) for root in allowed_roots):
            return None
        return path

    def _collections(self) -> dict[str, Path]:
        if self._collection_roots:
            return dict(self._collection_roots)
        try:
            payload = yaml.safe_load(self._index_config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            return {}
        collections = payload.get("collections") if isinstance(payload, dict) else None
        if not isinstance(collections, dict):
            return {}
        resolved: dict[str, Path] = {}
        for name, config in collections.items():
            if isinstance(config, dict) and config.get("path"):
                resolved[str(name)] = Path(str(config["path"])).expanduser().resolve()
        self._collection_roots = resolved
        return dict(resolved)

    def _refresh_queue_state(self) -> dict[str, int]:
        queue_root = self._root / ".agent" / "state" / "qmd-refresh"
        return {
            state: len(list((queue_root / state).glob("*.json")))
            for state in ("pending", "processing", "failed")
        }

    def _readiness_diagnostics(self) -> dict[str, Any]:
        if not self._config.enabled:
            return self._diagnostics_payload(
                status="disabled",
                available=False,
                degraded=False,
                reason="qmd backend is disabled",
                result_count=0,
            )
        if self._config.mode in {"vsearch", "query"} and not self._config.model_ready:
            return self._diagnostics_payload(
                status="model_not_ready",
                available=False,
                degraded=True,
                reason="model-backed qmd search requires explicit readiness",
                result_count=0,
            )
        if not self._index_config_path.is_file():
            return self._diagnostics_payload(
                status="not_configured",
                available=False,
                degraded=True,
                reason="repo-local qmd index config is missing",
                result_count=0,
            )
        if self._resolve_executable() is None:
            return self._diagnostics_payload(
                status="missing",
                available=False,
                degraded=True,
                reason="qmd executable was not found",
                result_count=0,
            )

        refresh = self._refresh_queue_state()
        stale = any(refresh.values())
        diagnostics = self._diagnostics_payload(
            status="stale" if stale else "configured",
            available=True,
            degraded=stale,
            reason="qmd refresh queue is not converged" if stale else None,
            result_count=0,
        )
        diagnostics.update(
            {
                "refresh_pending": refresh["pending"],
                "refresh_processing": refresh["processing"],
                "refresh_failed": refresh["failed"],
            }
        )
        return diagnostics

    def _diagnostics_payload(
        self,
        *,
        status: str,
        available: bool,
        degraded: bool,
        reason: str | None,
        result_count: int,
    ) -> dict[str, Any]:
        return {
            "name": "qmd",
            "backend": "qmd",
            "configured": self._config.enabled,
            "available": available,
            "status": status,
            "degraded": degraded,
            "reason": reason,
            "result_count": result_count,
        }
