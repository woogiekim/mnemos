"""Memory Gateway — single entry point for all memory operations."""
from __future__ import annotations

import datetime
import hashlib
import os
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any, Iterator

from core.contracts import RecallMemory, RecallReport
from core.policy import PolicyEngine, PolicyViolationError
from core.store import MemoryStore, StorageBackend
from core.log import AuditLogger
from core.hooks import HookDispatcher
from core.events import EventBus
from core.fts import FTSIndex
from core.observability import ObservabilityLogger
from core.search import SearchMiddleware
from core.config import get_backend_config, get_qmd_config


DEFAULT_QUALITY_SCORE = 0.8
_CONTENT_PREVIEW_LENGTH = 60


@contextmanager
def _timed_phase(phases: list[dict[str, Any]], name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        phases.append(
            {
                "name": name,
                "duration_ms": round(max(0.0, time.perf_counter() - started) * 1000, 3),
            }
        )


def _timed_capture(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        self._capture_phase_timings = []
        self._capture_store_diagnostics = {"enabled": False, "phases": []}
        qmd_enabled = bool(getattr(getattr(self, "_qmd_config", None), "enabled", False))
        self._qmd_refresh_diagnostics = {
            "enabled": qmd_enabled,
            "operation": "capture",
            "status": "not_requested" if qmd_enabled else "disabled",
            "queued": False,
            "worker_started": False,
            "error_code": None,
        }
        started = time.perf_counter()
        try:
            return method(self, *args, **kwargs)
        finally:
            self._capture_duration_ms = round(
                max(0.0, time.perf_counter() - started) * 1000,
                3,
            )

    return wrapped


# ---------------------------------------------------------------------------
# On-write dedup helpers
# ---------------------------------------------------------------------------

def _nfkc_normalise(content: str) -> str:
    """Apply NFKC Unicode normalisation, strip, collapse whitespace, lowercase.

    This is the canonical normalisation used for on-write dedup in
    :meth:`MemoryGateway.capture`.  It matches the same logic used by
    ``core.bg._normalise_content`` so background GC dedup and on-write dedup
    are consistent.

    Dedup key: ``(layer, SHA-256(nfkc_normalise(content)))``.
    Promotion across layers is NOT blocked by dedup — the key is
    layer-scoped so the same content can legitimately exist in ``session``
    and ``global`` simultaneously.
    """
    import re
    nfkc = unicodedata.normalize("NFKC", content)
    return re.sub(r"\s+", " ", nfkc.strip()).lower()


def _capture_content_hash(content: str) -> str:
    """Return SHA-256 hex digest of the NFKC-normalised content."""
    return hashlib.sha256(_nfkc_normalise(content).encode("utf-8")).hexdigest()


def _policy_candidates_for_root(root: Path) -> list[Path]:
    """Return the ordered list of conventional policy.yaml locations for *root*.

    This is the SAME candidate ordering used by :class:`MemoryGateway` when
    resolving the policy file at construction time:

    1. Install-root convention — ``root/wiki/policy.yaml``
       (used by ``~/.mnemos`` after a regular ``install.sh`` run).
    2. Dev/source-repo convention — ``root/repo/wiki/policy.yaml``
       (used when ``MNEMOS_REPO_ROOT`` points at a checked-out mnemos
       source tree, where the live policy lives at ``repo/wiki/policy.yaml``).

    The :envvar:`MNEMOS_POLICY_PATH` override is **not** included here —
    that override sits above this list in :class:`MemoryGateway`'s
    resolution sequence and is independent of the chosen repo root.
    """
    return [
        root / "wiki" / "policy.yaml",
        root / "repo" / "wiki" / "policy.yaml",
    ]


def _resolve_repo_root() -> Path:
    """Locate the mnemos repo root by checking, in order:

    1. The ``MNEMOS_REPO_ROOT`` environment variable (validated against
       both the install-root and dev/source-repo conventional layouts —
       see :func:`_policy_candidates_for_root`).
    2. Walking up from the current working directory.
    3. Walking up from this source file's location.

    The repo-root sanity check accepts either ``<root>/wiki/policy.yaml``
    (install layout) OR ``<root>/repo/wiki/policy.yaml`` (dev/source-repo
    layout), matching the candidate ordering applied later by
    :class:`MemoryGateway` when it picks the actual policy file. This
    ensures a dev checkout at ``/Users/wook/Developments/mnemos`` is
    accepted as ``MNEMOS_REPO_ROOT`` even though it has no top-level
    ``wiki/policy.yaml`` — the live file lives at
    ``repo/wiki/policy.yaml`` under that root.

    Raises ``FileNotFoundError`` with a human-readable message if none of
    the strategies succeed.
    """
    def _root_has_policy(p: Path) -> bool:
        return any(c.is_file() for c in _policy_candidates_for_root(p))

    # 1. Check MNEMOS_REPO_ROOT env var
    env_val = os.environ.get("MNEMOS_REPO_ROOT")
    if env_val:
        p = Path(env_val).expanduser().resolve()
        if _root_has_policy(p):
            return p
        # env var is set but does not point at a valid repo — fail immediately
        tried = ", ".join(str(c) for c in _policy_candidates_for_root(p))
        raise FileNotFoundError(
            f"MNEMOS_REPO_ROOT={env_val!r} does not contain policy.yaml "
            f"in any of: {tried}. "
            f"Set MNEMOS_POLICY_PATH to override the policy file location."
        )

    # 2. Walk up from CWD
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if _root_has_policy(parent):
            return parent

    # 3. Walk up from __file__
    here = Path(__file__).resolve()
    for parent in here.parents:
        if _root_has_policy(parent):
            return parent

    raise FileNotFoundError(
        "Cannot find mnemos repo root. "
        "Set the MNEMOS_REPO_ROOT environment variable to the repo path."
    )


_DEFAULT_LAYER = "ephemeral"


def _recall_score(result: dict[str, Any]) -> float:
    metadata = result.get("metadata") or {}
    for value in (
        result.get("operational_score"),
        metadata.get("operational_score"),
        result.get("score"),
    ):
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _recall_score_without_legacy_history(result: dict[str, Any]) -> float:
    metadata = result.get("metadata") or {}
    components = metadata.get("score_components") or {}
    score = _recall_score(result)
    try:
        legacy_historical = float(components.get("historical") or 0.0)
    except (TypeError, ValueError):
        legacy_historical = 0.0
    return max(0.0, score - (legacy_historical * 0.1))


def _as_tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (list, tuple, set)):
        return tuple(str(value) for value in values)
    return (str(values),)


def _has_contradiction(item: dict[str, Any]) -> bool:
    tags = {str(tag).lower() for tag in item.get("tags") or []}
    return bool(item.get("contradiction") or "contradiction" in tags or "invalidated" in tags)


def _project_specific(item: dict[str, Any]) -> bool:
    if item.get("project_root_hash"):
        return True
    for key in ("source_path", "source_section"):
        value = str(item.get(key) or "")
        if value.startswith(("/", "apps/", "src/", "core/", "agents/", "tests/")):
            return True
    tags = {str(tag).lower() for tag in item.get("tags") or []}
    return "project-specific" in tags or "project_specific" in tags


def _recall_memory(
    item: dict[str, Any],
    *,
    score: float,
    matched_queries: tuple[str, ...],
    source: str | None,
) -> RecallMemory:
    item_id = str(item.get("id") or Path(str(item.get("_path", ""))).stem)
    return RecallMemory(
        id=item_id,
        layer=str(item.get("layer") or ""),
        stage=str(item.get("stage") or ""),
        content=str(item.get("content") or ""),
        score=round(score, 6),
        matched_queries=matched_queries,
        tags=_as_tuple(item.get("tags")),
        project_id=item.get("project_id"),
        project_root_hash=item.get("project_root_hash"),
        semantic_status=item.get("semantic_status"),
        task_shape=item.get("task_shape"),
        agent_role=item.get("agent_role"),
        active_files=_as_tuple(item.get("active_files")),
        created_at=item.get("created_at"),
        updated_at=item.get("updated_at"),
        source=source,
        source_revision=item.get("source_revision"),
        source_path=item.get("source_path"),
        source_section=item.get("source_section"),
        provenance=dict(item.get("provenance") or {}),
        record_type=item.get("record_type") or item.get("sourceType"),
        score_components=dict(item.get("score_components") or {}),
    )


def _select_recall_memories(
    candidates: list[RecallMemory],
    *,
    selected_limit: int,
    max_selected_chars: int,
) -> tuple[list[RecallMemory], int]:
    selected: list[RecallMemory] = []
    used_chars = 0
    for candidate in candidates:
        if len(selected) >= selected_limit:
            break

        remaining = max_selected_chars - used_chars
        if remaining <= 0:
            break

        content = candidate.content
        if len(content) > remaining:
            if remaining <= 3:
                content = "." * remaining
            else:
                content = content[: remaining - 3].rstrip() + "..."

        used_chars += len(content)
        selected.append(replace(candidate, content=content))

    return selected, used_chars


class MemoryGateway:
    """
    Single entry point for all memory lifecycle operations.

    All mutations are validated by PolicyEngine, persisted by MemoryStore,
    logged by AuditLogger, and trigger HookDispatcher events.

    On construction a *run_id* and *session_id* are auto-generated (UUID4) so
    that ephemeral/working items written in this process share a coherent
    namespace.  Callers may override these per-call via the ``run_id`` and
    ``session_id`` keyword arguments on :meth:`capture`, :meth:`promote`, and
    :meth:`demote`.
    """

    def __init__(self, repo_root: str | None = None) -> None:
        self._root = str(repo_root) if repo_root else str(_resolve_repo_root())

        # Resolve policy.yaml using an ordered candidate search. The override
        # is a *preference*, not a hard requirement: if MNEMOS_POLICY_PATH
        # points at a non-existent file, fall through to the conventional
        # locations rather than crash. This matches the principle "I gave
        # a hint; if it's wrong, still try the defaults."
        #
        # Resolution order:
        #   1. MNEMOS_POLICY_PATH env override (if it points at a real file)
        #   2. Install-root convention      — ``<root>/wiki/policy.yaml``
        #   3. Dev/source-repo convention   — ``<root>/repo/wiki/policy.yaml``
        #
        # The dev/source-repo fallback exists because a mnemos checkout used
        # as MNEMOS_REPO_ROOT keeps the live policy under ``repo/wiki/`` (the
        # ``~/.mnemos`` install layout has the file at the top-level ``wiki/``,
        # but a source-tree checkout does not).
        root_path = Path(self._root)
        candidates: list[Path] = []
        override = os.environ.get("MNEMOS_POLICY_PATH")
        if override:
            candidates.append(Path(override).expanduser())
        candidates.extend(_policy_candidates_for_root(root_path))

        policy_path = next((str(p) for p in candidates if p.is_file()), None)
        if policy_path is None:
            tried = ", ".join(str(p) for p in candidates)
            raise FileNotFoundError(
                f"policy.yaml not found in any of: {tried}. "
                f"Set MNEMOS_POLICY_PATH to override."
            )

        self._policy = PolicyEngine(policy_path=policy_path)
        self._logger = AuditLogger(repo_root=self._root)
        self._hooks = HookDispatcher(repo_root=self._root)
        self._event_bus: EventBus = EventBus()
        self._obs = ObservabilityLogger(repo_root=self._root)
        state_dir = Path(self._root) / ".agent" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        fts_db = str(state_dir / "fts.db")
        self._fts = FTSIndex(db_path=fts_db)

        # ── Backend selection ──────────────────────────────────────────────
        # Priority: MNEMOS_BACKEND env var > mnemos.yml storage.backend > default
        # The default backend (MemoryStore) is unchanged and remains opt-in.
        backend_cfg = get_backend_config(repo_root=self._root)
        self._qmd_config = get_qmd_config(repo_root=self._root)
        if backend_cfg.backend == "obsidian":
            from core.obsidian import ObsidianBackend
            vault_path = backend_cfg.vault_path
            if not vault_path:
                raise ValueError(
                    "MNEMOS_BACKEND=obsidian requires storage.vault_path in "
                    "mnemos.yml (or set MNEMOS_VAULT_PATH env var)"
                )
            self._store: StorageBackend = ObsidianBackend(
                vault_path=vault_path,
                fts=self._fts,
                sync_config=backend_cfg.sync,
            )
        else:
            # Default backend (issue #69): optional remote git-sync, opt-in
            # only.  ``backend_cfg.sync.enabled`` is True only when the user
            # explicitly set ``storage.sync.enabled: true`` — the default
            # backend is never auto-enabled (see core/config.py).  When sync is
            # disabled the engine is inert and behaviour matches every release
            # before #69.
            self._store = MemoryStore(
                repo_root=self._root,
                sync_config=backend_cfg.sync,
            )

        self._search = SearchMiddleware(
            repo_root=self._root,
            fts_index=self._fts,
            store=self._store,
        )

        # ── Automatic distillation (#87) ─────────────────────────────────
        # Cache the resolved config on the instance and register the
        # ``post-capture`` subscriber so distill fires every N captures.
        # When ``enabled=False``, NO subscriber is registered, so the
        # event bus's ``handler_count("post-capture")`` is unchanged — the
        # opt-out is observable from outside.
        self._distill_enabled: bool = backend_cfg.distillation.enabled
        self._distill_interval: int = backend_cfg.distillation.interval_captures
        self._in_auto_distill: bool = False
        if self._distill_enabled:
            self._register_auto_distill_subscriber()

        # Auto-generated IDs scoped to this gateway instance (i.e. this process)
        self._run_id: str = str(uuid.uuid4())
        self._session_id: str = str(uuid.uuid4())
        # On-write dedup registry: maps (layer, content_hash) → item_id.
        # Keyed by layer so cross-layer promotion is never blocked.
        self._capture_dedup: dict[tuple[str, str], str] = {}
        # Set to True when capture() detects a cross-process duplicate via
        # persistent storage scan. Reset to False at the start of each capture()
        # call. The CLI reads this flag to emit "(existing) <uuid>" instead of
        # the normal "captured: <uuid>" line.
        self.last_capture_was_duplicate: bool = False
        self._capture_phase_timings: list[dict[str, Any]] = []
        self._capture_store_diagnostics: dict[str, Any] = {
            "enabled": False,
            "phases": [],
        }
        self._qmd_refresh_diagnostics: dict[str, Any] = {
            "enabled": self._qmd_config.enabled,
            "operation": None,
            "status": "not_requested" if self._qmd_config.enabled else "disabled",
            "queued": False,
            "worker_started": False,
            "error_code": None,
        }
        self._capture_duration_ms: float = 0.0

    # ------------------------------------------------------------------ #
    # Observability logger access                                           #
    # ------------------------------------------------------------------ #

    @property
    def observability(self) -> ObservabilityLogger:
        """Return the gateway's :class:`~core.observability.ObservabilityLogger`.

        CLI commands (``mnemos audit``, ``mnemos stats``) and hooks read from
        and write to this logger directly.
        """
        return self._obs

    @property
    def last_capture_diagnostics(self) -> dict[str, Any]:
        """Return content-free timings from the most recent capture call."""
        return {
            "duration_ms": self._capture_duration_ms,
            "phases": [dict(phase) for phase in self._capture_phase_timings],
            "store": {
                "enabled": bool(self._capture_store_diagnostics.get("enabled")),
                "phases": [
                    dict(phase)
                    for phase in self._capture_store_diagnostics.get("phases", [])
                    if isinstance(phase, dict)
                ],
            },
            "qmd_refresh": dict(self._qmd_refresh_diagnostics),
        }

    @property
    def last_qmd_refresh_diagnostics(self) -> dict[str, Any]:
        """Return content-free status from the latest derived-index enqueue."""
        return dict(self._qmd_refresh_diagnostics)

    def _enqueue_qmd_refresh(self, reason: str) -> None:
        """Schedule optional derived indexing without affecting canonical work."""
        if not self._qmd_config.enabled:
            self._qmd_refresh_diagnostics = {
                "enabled": False,
                "operation": reason,
                "status": "disabled",
                "queued": False,
                "worker_started": False,
                "error_code": None,
            }
            return
        try:
            from core.qmd_queue import enqueue_qmd_refresh

            result = enqueue_qmd_refresh(
                repo_root=self._root,
                reason=reason,
                config=self._qmd_config,
            )
        except Exception as exc:
            self._qmd_refresh_diagnostics = {
                "enabled": True,
                "operation": reason,
                "status": "enqueue_failed",
                "queued": False,
                "worker_started": False,
                "error_code": exc.__class__.__name__,
            }
            return

        queued = bool(getattr(result, "queued", False))
        worker_started = bool(getattr(result, "worker_started", False))
        worker_error_code = getattr(result, "worker_error_code", None)
        self._qmd_refresh_diagnostics = {
            "enabled": True,
            "operation": reason,
            "status": "queued" if queued else "not_queued",
            "queued": queued,
            "worker_started": worker_started,
            "error_code": str(worker_error_code) if worker_error_code else None,
        }

    # ------------------------------------------------------------------ #
    # Event bus access                                                      #
    # ------------------------------------------------------------------ #

    @property
    def event_bus(self) -> EventBus:
        """Return the gateway's :class:`~core.events.EventBus` instance.

        Adapters call ``gateway.event_bus.subscribe(event, handler)`` to
        register in-process notification handlers.  The bus is created once
        per gateway instance and shared across all callers.
        """
        return self._event_bus

    # ------------------------------------------------------------------ #
    # Internal: silent auto-promotion                                        #
    # ------------------------------------------------------------------ #

    def _auto_promote_if_eligible(
        self,
        item_id: str,
        item: dict[str, Any],
    ) -> None:
        """Silently promote *item* if it meets promotion thresholds.

        This is called as a side-effect after capture/search/read operations.
        It never raises and emits a ``post-promote`` event on the :attr:`event_bus`
        so that adapter handlers (e.g. ClaudeCode stdout notice) are notified
        even for auto-promotions that were previously completely silent.
        """
        try:
            if not self._policy.check_promotion_eligible(item):
                return
            next_layer = self._policy.get_next_layer(item.get("layer", ""))
            if next_layer is None:
                return
            self.promote(item_id=item_id, target_layer=next_layer)
            # Note: promote() already emits the post-promote event via EventBus,
            # so no additional emit is needed here.
        except Exception:
            # Swallow all errors: auto-promotion is best-effort
            pass

    # ------------------------------------------------------------------ #
    # Cross-process dedup helper                                            #
    # ------------------------------------------------------------------ #

    def _find_existing_by_hash(
        self, content_hash: str
    ) -> tuple[str, str] | None:
        """Scan ALL layers for a memory item whose ``content_hash`` metadata
        field matches *content_hash*.

        Returns ``(item_id, layer)`` of the first matching item found, or
        ``None`` when no match exists.

        This is the cross-process deduplication guard introduced to fix
        issues #49 and #50.  The in-memory ``_capture_dedup`` registry only
        covers the current process lifetime; this method performs a
        filesystem scan so that a second invocation of ``mnemos capture``
        in a fresh process finds the item written by the first invocation.

        Scanning order: static layers (project, global, entities, claims,
        topics) first, then dynamic layers (session, ephemeral, working,
        transient).  The scan short-circuits on the first match to keep
        latency bounded for typical small stores.
        """
        indexed_lookup = getattr(self._store, "find_by_content_hash", None)
        if callable(indexed_lookup):
            return indexed_lookup(content_hash)

        from core.layers import LAYER_STATIC_PATHS

        static_layers = list(LAYER_STATIC_PATHS.keys())
        dynamic_layers = ["session", "ephemeral", "working", "transient"]
        all_layers = static_layers + dynamic_layers

        for layer in all_layers:
            try:
                for item in self._store.iter_layer_items(layer):
                    stored_hash = item.get("content_hash")
                    if stored_hash and stored_hash == content_hash:
                        raw_path = item.get("_path", "")
                        item_id = str(
                            item.get("id") or (Path(raw_path).stem if raw_path else "")
                        )
                        if item_id:
                            return (item_id, layer)
            except Exception:
                # iter_layer_items is best-effort — skip layers that error
                continue

        return None

    # ------------------------------------------------------------------ #
    # Automatic distillation (#87)                                          #
    # ------------------------------------------------------------------ #

    def _register_auto_distill_subscriber(self) -> None:
        """Subscribe the post-capture distill handler on the in-process bus.

        Called from :meth:`__init__` only when ``distillation.enabled`` is
        ``True``. The handler is registered on the Python event bus
        (:class:`core.events.EventBus`), NOT on the shell-script
        :class:`core.hooks.HookDispatcher` — the bus delivers to in-process
        callables, the dispatcher only runs ``.agent/workflows/hooks/*``
        shell scripts.
        """
        self._event_bus.subscribe("post-capture", self._on_post_capture_distill)

    def _on_post_capture_distill(self, payload: dict) -> None:
        """Bump the durable counter so maintenance can drain due distillation.

        Wrapped end-to-end in ``try/except Exception`` so a corrupt sidecar,
        a planning bug, or a downstream apply error can never propagate to the
        original ``capture()`` caller. The re-entrancy guard
        (``self._in_auto_distill``) prevents the recursive ``gateway.capture``
        the apply path issues for the artifact body from mutating the due
        counter while a maintenance run is already draining it.
        """
        if self._in_auto_distill:
            return

        from core.distill import (
            _read_distill_state,
            _state_path,
            _write_distill_state,
        )

        try:
            state_path = _state_path()
            state = _read_distill_state(state_path)
            counter_before = int(state.get("captures_since_last_distill", 0))
            new_counter = counter_before + 1
            state["captures_since_last_distill"] = new_counter
            _write_distill_state(state_path, state)
        except Exception as exc:  # noqa: BLE001
            # Last-resort swallow: e.g. a sidecar read raised an unexpected
            # type and slipped past the inner guards. Log via observability,
            # but never propagate to ``capture()``.
            try:
                self._obs.log_auto_distill(
                    success=False,
                    error=str(exc),
                    trigger="post-capture",
                    interval=self._distill_interval,
                )
            except Exception:  # pragma: no cover - observability is best-effort
                pass

    def _reset_distill_counter(self) -> None:
        """Rewrite the sidecar with a zero counter and a fresh ``last_distill_at``.

        Used by :meth:`consolidate` after a successful sweep-end distill so
        that the subscriber's counter and the consolidate-triggered counter
        stay synchronised.
        """
        from core.distill import _state_path, _write_distill_state

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_distill_state(
            _state_path(),
            {
                "captures_since_last_distill": 0,
                "last_distill_at": now_iso,
            },
        )

    # ------------------------------------------------------------------ #
    # Capture                                                               #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Auto-classify                                                         #
    # ------------------------------------------------------------------ #

    def auto_classify(
        self,
        item_id: str,
        content: str,
        *,
        schedule_qmd_refresh: bool = True,
    ) -> list[str]:
        """Derive and assign at least one tag automatically from *content*.

        Tags are derived from simple keyword matching across several
        semantic categories.  At least one tag is always assigned — the
        catch-all ``"general"`` tag is appended when no category keyword
        matches.

        The method is idempotent: tags that already exist on the item are
        preserved; only genuinely new tags are added.

        Returns the list of newly-added tags (may be empty if all derived
        tags were already present).
        """
        import re

        text = content.lower()

        # Keyword → tag mapping (ordered; first match wins per category).
        CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
            ("architecture", [
                "architecture", "design pattern", "hexagonal", "ddd",
                "domain model", "microservice", "monolith", "service",
                "protocol", "interface", "abstraction",
            ]),
            ("decision", [
                "decided", "decision", "decided to", "we chose", "we will use",
                "chosen", "rationale", "trade-off", "tradeoff",
            ]),
            ("constraint", [
                "constraint", "must not", "must always", "never", "forbidden",
                "required by", "enforced", "invariant", "rule:",
            ]),
            ("bug", [
                "bug", "error", "exception", "traceback", "crash", "failure",
                "root cause", "fix:", "workaround",
            ]),
            ("performance", [
                "performance", "latency", "throughput", "slow", "fast",
                "cache", "index", "query", "benchmark", "ms", "seconds",
            ]),
            ("security", [
                "security", "auth", "authentication", "authorisation",
                "authorization", "token", "secret", "credential", "csrf",
                "xss", "injection",
            ]),
            ("testing", [
                "test", "pytest", "unittest", "assertion", "mock", "fixture",
                "tdd", "coverage", "spec", "should ",
            ]),
            ("workflow", [
                "workflow", "pipeline", "step", "phase", "process", "hook",
                "script", "automation", "ci", "cd", "deploy",
            ]),
            ("preference", [
                "prefer", "preference", "user wants", "always use",
                "from now on", "in this project", "convention",
            ]),
            ("project", [
                "project", "feature", "milestone", "sprint", "backlog",
                "issue", "ticket", "pr", "pull request",
            ]),
        ]

        # Collect matched categories
        matched: list[str] = []
        for tag_label, keywords in CATEGORY_KEYWORDS:
            pattern = r"|".join(re.escape(kw) for kw in keywords)
            if re.search(pattern, text):
                matched.append(tag_label)

        # Guarantee at least one tag
        if not matched:
            matched = ["general"]

        # Read current tags and add only new ones
        try:
            item = self._store.read(item_id)
            existing_tags: list[str] = list(item.get("tags") or [])
        except Exception:
            existing_tags = []

        new_tags = [t for t in matched if t not in existing_tags]
        if new_tags:
            all_tags = existing_tags + new_tags
            try:
                item = self._store.read(item_id)
                self._store.update(
                    item["_path"],
                    metadata_updates={"tags": all_tags, "stage": "classified"},
                )
                self._logger.append(
                    "auto_classify",
                    item_id,
                    item.get("layer", "unknown"),
                    {"tags_added": new_tags},
                )
                # Update FTS index with new tags
                self._fts.index_item(
                    item_id=item_id,
                    content=item.get("content", ""),
                    metadata={"layer": item.get("layer", ""), "tags": all_tags},
                )
                if schedule_qmd_refresh:
                    self._enqueue_qmd_refresh("auto_classify")
            except Exception:
                # Auto-classify is best-effort — never fail a capture
                pass

        return new_tags

    @_timed_capture
    def capture(
        self,
        content: str,
        layer: str | None = None,
        item_id: str | None = None,
        tags: list[str] | None = None,
        quality_score: float = DEFAULT_QUALITY_SCORE,
        run_id: str | None = None,
        session_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        no_classify: bool = False,
    ) -> str | None:
        """Capture a new memory item into the target layer.

        When *layer* is omitted it defaults to ``"ephemeral"``.  The
        *run_id* and *session_id* are filled from this gateway instance's
        auto-generated values when not supplied by the caller, ensuring
        that ephemeral items always land in a deterministic path for the
        duration of the process.

        On-write dedup (in-process)
        ---------------------------
        Before writing, the content is NFKC-normalised and hashed.  The
        dedup key is ``(layer, SHA-256(nfkc_normalise(content)))``.  If an
        item with the same key was already captured by **this gateway
        instance** in the current process, the call is a silent no-op and
        returns ``None``.

        **Why per-instance?**  The dedup registry lives in memory so it does
        not require a cross-process lock or a full store scan.  It covers the
        most common flooding scenario — a Stop hook firing once per AI
        response turn and re-submitting identical content — without adding
        significant latency to normal captures.

        Cross-process dedup (persistent storage scan — Issues #49/#50)
        ---------------------------------------------------------------
        After the in-process check, the content hash is looked up across ALL
        layers in the persistent store.  If found, the existing item_id is
        returned and ``self.last_capture_was_duplicate`` is set to ``True``
        so that the CLI can print ``(existing) <uuid>`` instead of the normal
        capture notice.  The in-memory registry is also updated with the
        existing id so subsequent in-process calls are blocked without
        another store scan.

        **Cross-layer promotion is not blocked.**  The in-process dedup key is
        layer-scoped, so capturing the same content into ``session`` after
        it has already been promoted to ``global`` is allowed.  However, the
        cross-process scan checks ALL layers, which means the same content
        already stored in ANY layer will be detected as a duplicate across
        different processes — this matches the requirement:
        ``dedup_scope: Cross-layer``.
        """
        dedup_started = time.perf_counter()

        def record_dedup_timing() -> None:
            self._capture_phase_timings.append(
                {
                    "name": "dedup_lookup",
                    "duration_ms": round(
                        max(0.0, time.perf_counter() - dedup_started) * 1000,
                        3,
                    ),
                }
            )

        # Reset the duplicate flag for this call
        self.last_capture_was_duplicate = False

        if layer is None:
            layer = _DEFAULT_LAYER

        # Fill dynamic IDs from gateway defaults when not explicitly provided
        if run_id is None:
            run_id = self._run_id
        if session_id is None:
            session_id = self._session_id

        # ------------------------------------------------------------------
        # On-write dedup check (Issue #4 anti-regression — in-process path)
        # ------------------------------------------------------------------
        # This gate makes Stop hook re-introduction safe: a Stop event that
        # fires per AI response turn will parse the same capture markers from
        # the conversation transcript on every turn.  Without dedup, that
        # floods the session layer with identical items.  With dedup, the
        # second and subsequent identical captures within the same gateway
        # process are silent no-ops — the write path (store, FTS, audit log,
        # hooks) is never reached.
        content_hash = _capture_content_hash(content)
        dedup_key = (layer, content_hash)
        if dedup_key in self._capture_dedup:
            record_dedup_timing()
            return None

        # ------------------------------------------------------------------
        # Cross-process dedup check (Issues #49/#50 — persistent storage scan)
        # ------------------------------------------------------------------
        # A second invocation of `mnemos capture` in a new process starts with
        # an empty _capture_dedup registry.  Scan the persistent store across
        # all layers to detect items written by previous processes.  Short-
        # circuits on first match so typical small stores see minimal overhead.
        #
        # Guard: only fire the persistent scan when no entry for this content
        # hash exists in the in-process registry under ANY layer.  This preserves
        # the existing cross-layer same-process behaviour (e.g. capturing the
        # same content to 'session' and then to 'global' in a single process is
        # still allowed and produces a new item_id — promotion use-case).
        hash_already_in_process = any(
            h == content_hash for (_, h) in self._capture_dedup
        )
        if not hash_already_in_process:
            existing = self._find_existing_by_hash(content_hash)
            if existing is not None:
                existing_id, _existing_layer = existing
                # Warm the in-process cache so subsequent same-process calls are
                # fast (no second scan) and return None per the in-process contract.
                self._capture_dedup[dedup_key] = existing_id
                self.last_capture_was_duplicate = True
                record_dedup_timing()
                return existing_id

        record_dedup_timing()

        with _timed_phase(self._capture_phase_timings, "policy_validation"):
            self._policy.validate_capture(layer=layer, item={"content": content})

        item_id = item_id or str(uuid.uuid4())
        # Register in the dedup cache immediately — before the write — so that
        # a concurrent duplicate call in the same process is also blocked even
        # if the write is still in progress.
        self._capture_dedup[dedup_key] = item_id
        now = datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"

        metadata: dict[str, Any] = {
            "id": item_id,
            "layer": layer,
            "stage": "stored",
            "created_at": now,
            "access_count": 0,
            "quality_score": quality_score,
            "tags": tags or [],
            "run_id": run_id,
            "session_id": session_id,
            # Store the content hash in metadata so _find_existing_by_hash()
            # can locate this item in future cross-process scans without
            # re-hashing every item's content.
            "content_hash": content_hash,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            with _timed_phase(self._capture_phase_timings, "store_write"):
                self._store.write(
                    layer=layer,
                    item_id=item_id,
                    content=content,
                    metadata=metadata,
                    run_id=run_id,
                    session_id=session_id,
                )
        finally:
            sync_engine = getattr(self._store, "_sync_engine", None)
            store_diagnostics = getattr(sync_engine, "last_write_diagnostics", None)
            if isinstance(store_diagnostics, dict):
                self._capture_store_diagnostics = store_diagnostics

        with _timed_phase(self._capture_phase_timings, "fts_index"):
            self._fts.index_item(
                item_id=item_id,
                content=content,
                metadata={"layer": layer, "tags": tags or []},
            )

        with _timed_phase(self._capture_phase_timings, "side_effects"):
            self._logger.append(
                operation="capture",
                item_id=item_id,
                layer=layer,
                metadata={"tags": tags or []},
            )
            content_preview = content[:_CONTENT_PREVIEW_LENGTH]
            self._hooks.fire("post-capture", {"item_id": item_id, "layer": layer})
            self._event_bus.emit("post-capture", {
                "item_id": item_id,
                "content_preview": content_preview,
                "layer": layer,
            })

            # Observability: record this capture event (async, non-blocking)
            self._obs.log_capture(
                memory_id=item_id,
                layer=layer,
                tags=tags or [],
                session_id=session_id,
            )

        # Auto-classify: derive tags automatically after write, unless opted out.
        # This runs after the observability log so the initial capture event
        # reflects pre-classify state; classify adds tags in a separate update.
        with _timed_phase(self._capture_phase_timings, "classification"):
            if not no_classify:
                self.auto_classify(
                    item_id=item_id,
                    content=content,
                    schedule_qmd_refresh=False,
                )

        self._enqueue_qmd_refresh("capture")

        return item_id

    # ------------------------------------------------------------------ #
    # Classify                                                              #
    # ------------------------------------------------------------------ #

    def classify(
        self,
        item_id: str,
        tag: str,
        layer: str | None = None,
    ) -> None:
        """Add a tag to an existing memory item."""
        item = self._store.read(item_id)
        tags = item.get("tags", [])
        if tag not in tags:
            tags.append(tag)
        self._store.update(item["_path"], metadata_updates={"tags": tags, "stage": "classified"})
        self._logger.append("classify", item_id, item.get("layer", "unknown"), {"tag": tag})
        self._enqueue_qmd_refresh("classify")

    # ------------------------------------------------------------------ #
    # Search                                                                #
    # ------------------------------------------------------------------ #

    def search(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
        tags: list[str] | None = None,
        touch: bool = False,
    ) -> list[dict[str, Any]]:
        """Search across memory layers.

        By default this is read-only.  ``touch=True`` preserves the legacy
        access_count update, but search never performs auto-promotion.

        Args:
            tags: When provided, only items whose stored tag list contains ALL
                  of the specified tags are returned (AND logic).
            touch: When True, increment legacy access_count for each hit.
        """
        # Fetch more results than needed when tag filtering so post-filter can
        # still satisfy the limit.
        fetch_limit = limit * 5 if tags else limit
        results = self._search.search(query=query, layers=layers, limit=fetch_limit)

        # Post-filter by tags (AND logic): every specified tag must be present.
        if tags:
            filtered = []
            for result in results:
                result_item_id = result.get("item_id")
                if not result_item_id:
                    continue
                try:
                    item = self._store.read(result_item_id)
                    item_tags = set(item.get("tags") or [])
                    if all(t in item_tags for t in tags):
                        filtered.append(result)
                        if len(filtered) >= limit:
                            break
                except Exception:
                    continue
            results = filtered

        if touch:
            self._touch_search_results(results)

        # Observability: record this search event (async, non-blocking)
        self._obs.log_search(
            keywords=[query],
            results=results,
            session_id=self._session_id,
        )

        return results

    def _touch_search_results(self, results: list[dict[str, Any]]) -> None:
        """Increment legacy access_count for explicit ``search --touch``."""
        for result in results:
            result_item_id = result.get("item_id")
            if not result_item_id:
                continue
            try:
                item = self._store.read(result_item_id)
                new_count = item.get("access_count", 0) + 1
                self._store.update(
                    item["_path"],
                    metadata_updates={"access_count": new_count},
                )
            except Exception:
                pass

    def search_for_context(
        self,
        *,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
        allow_grep: bool = False,
    ) -> list[dict[str, Any]]:
        """Search for host context injection without mutating memory metadata.

        Prompt hooks must stay on a read-only fast path: no access_count bump,
        no auto-promotion, and no storage update/git-sync side effects.
        """
        results = self._search.search(
            query=query,
            layers=layers,
            limit=limit,
            allow_grep=allow_grep,
        )
        self._obs.log_search(
            keywords=[query],
            results=results,
            session_id=self._session_id,
        )
        return results

    def recall(
        self,
        *,
        queries: list[str],
        layers: list[str] | None = None,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        project_id: str | None = None,
        project_root_hash: str | None = None,
        semantic_statuses: list[str] | None = None,
        task_shape: str | None = None,
        agent_role: str | None = None,
        active_files: list[str] | None = None,
        candidate_limit: int = 20,
        selected_limit: int = 6,
        max_selected_chars: int = 3600,
    ) -> RecallReport:
        """Return read-only recall candidates and selected memories.

        This path intentionally uses :meth:`search_for_context` plus
        :meth:`peek`, never :meth:`search` or :meth:`read`, so candidate lookup
        is not counted as memory use and cannot trigger auto-promotion.
        """
        normalized_queries = tuple(query for query in queries if query.strip())
        candidate_limit = max(0, candidate_limit)
        selected_limit = max(0, selected_limit)
        max_selected_chars = max(0, max_selected_chars)

        if not normalized_queries or candidate_limit == 0:
            return RecallReport(
                queries=normalized_queries,
                candidates=(),
                selected=(),
                candidate_limit=candidate_limit,
                selected_limit=selected_limit,
                max_selected_chars=max_selected_chars,
                used_chars=0,
                diagnostics={"attempts": []},
            )

        fetch_limit = max(candidate_limit, selected_limit, 1)
        from core.feedback import FeedbackStore, is_recall_suppressed, validated_usage_score

        usage_projection = FeedbackStore(self._root).read_projection()
        by_id: dict[str, dict[str, Any]] = {}
        diagnostics: list[dict[str, Any]] = []
        for query in normalized_queries:
            results = self.search_for_context(
                query=query,
                layers=layers,
                limit=fetch_limit,
                allow_grep=False,
            )
            diagnostics.append({"query": query, **self.last_search_diagnostics})

            for result in results:
                item_id = str(result.get("item_id") or result.get("id") or "")
                if not item_id:
                    continue
                try:
                    item = dict(self.peek(item_id))
                except Exception:
                    continue
                result_metadata = result.get("metadata") or {}
                if result_metadata.get("score_components"):
                    item["score_components"] = dict(result_metadata.get("score_components") or {})
                if not self._matches_recall_filters(
                    item,
                    layers=layers,
                    tags_all=tags_all,
                    tags_any=tags_any,
                    project_id=project_id,
                    project_root_hash=project_root_hash,
                    semantic_statuses=semantic_statuses,
                    task_shape=task_shape,
                    agent_role=agent_role,
                    active_files=active_files,
                ):
                    continue
                usage = usage_projection.get(item_id, {})
                if item.get("superseded_by") or item.get("semantic_status") == "invalidated" or is_recall_suppressed(usage):
                    continue

                result_metadata = result.get("metadata") or {}
                result_components = dict(result_metadata.get("score_components") or {})
                usage_score = validated_usage_score(usage)
                score = min(1.0, _recall_score_without_legacy_history(result) + (usage_score * 0.1))
                item["score_components"] = {
                    **result_components,
                    "historical": usage_score,
                    "validated_usage": usage_score,
                    "applied_count": int(usage.get("applied_count") or 0),
                    "validated_use_count": int(usage.get("validated_use_count") or 0),
                    "distinct_validated_task_count": int(usage.get("distinct_validated_task_count") or 0),
                    "retrieval_count": int(usage.get("retrieval_count") or 0),
                    "selected_count": int(usage.get("selected_count") or 0),
                    "legacy_access_count": int(usage.get("legacy_access_count") or item.get("access_count") or 0),
                }
                existing = by_id.get(item_id)
                if existing is None:
                    by_id[item_id] = {
                        "item": item,
                        "score": score,
                        "queries": [query],
                        "source": result.get("source"),
                    }
                    continue

                existing["score"] = max(float(existing["score"]), score)
                if query not in existing["queries"]:
                    existing["queries"].append(query)

        candidates = [
            _recall_memory(
                data["item"],
                score=float(data["score"]),
                matched_queries=tuple(data["queries"]),
                source=data.get("source"),
            )
            for data in by_id.values()
        ]
        candidates.sort(key=lambda item: (-item.score, item.id))
        candidates = candidates[:candidate_limit]

        selected, used_chars = _select_recall_memories(
            candidates,
            selected_limit=selected_limit,
            max_selected_chars=max_selected_chars,
        )

        return RecallReport(
            queries=normalized_queries,
            candidates=tuple(candidates),
            selected=tuple(selected),
            candidate_limit=candidate_limit,
            selected_limit=selected_limit,
            max_selected_chars=max_selected_chars,
            used_chars=used_chars,
            diagnostics={"attempts": diagnostics},
        )

    def evaluate_feedback_promotion(self, memory_id: str) -> bool:
        """Promote only after applied/validated feedback proves real use."""
        from core.feedback import FeedbackStore

        store = FeedbackStore(self._root)
        events = store.read_events()
        projection = store.read_projection()
        usage = projection.get(memory_id, {})
        try:
            item = self.peek(memory_id)
        except Exception:
            return False

        if item.get("semantic_status") not in (None, "active", "verified"):
            return False
        if item.get("superseded_by") or usage.get("invalidated") or usage.get("superseded"):
            return False
        if _has_contradiction(item):
            return False

        layer = item.get("layer")
        if layer == "session":
            if int(usage.get("distinct_applied_task_count") or 0) < 2:
                return False
            if int(usage.get("validated_use_count") or 0) < 1:
                return False
            project_ids = {
                str(event.get("project_id"))
                for event in events
                if event.get("memory_id") == memory_id
                and event.get("event") in {"applied", "validated"}
                and event.get("project_id")
            }
            item_project_id = item.get("project_id")
            if item_project_id and project_ids and project_ids != {str(item_project_id)}:
                return False
            self.promote(memory_id, target_layer="project", force=True)
            return True

        if layer == "project":
            if int(usage.get("distinct_validated_project_count") or 0) < 2:
                return False
            if _project_specific(item):
                return False
            self.promote(memory_id, target_layer="global", force=True)
            return True

        return False

    def _matches_recall_filters(
        self,
        item: dict[str, Any],
        *,
        layers: list[str] | None,
        tags_all: list[str] | None,
        tags_any: list[str] | None,
        project_id: str | None,
        project_root_hash: str | None,
        semantic_statuses: list[str] | None,
        task_shape: str | None,
        agent_role: str | None,
        active_files: list[str] | None,
    ) -> bool:
        if layers is not None and item.get("layer") not in layers:
            return False

        item_tags = {str(tag) for tag in item.get("tags") or []}
        if tags_all and not all(tag in item_tags for tag in tags_all):
            return False
        if tags_any and not any(tag in item_tags for tag in tags_any):
            return False

        if project_id is not None and item.get("project_id") != project_id:
            return False
        if project_root_hash is not None and item.get("project_root_hash") != project_root_hash:
            return False
        if semantic_statuses is not None and item.get("semantic_status") not in semantic_statuses:
            return False
        if task_shape is not None and item.get("task_shape") != task_shape:
            return False
        if agent_role is not None and item.get("agent_role") != agent_role:
            return False

        stored_active_files = {str(path) for path in item.get("active_files") or []}
        if active_files and stored_active_files and not stored_active_files.intersection(active_files):
            return False

        return True

    @property
    def last_search_diagnostics(self) -> dict[str, Any]:
        """Return backend diagnostics from the most recent search."""
        return self._search.last_diagnostics

    def retrieval_backend_health(self) -> dict[str, Any]:
        """Return operational health for retrieval backends and fallbacks."""
        return self._search.backend_health()

    # ------------------------------------------------------------------ #
    # Read                                                                  #
    # ------------------------------------------------------------------ #

    def peek(self, item_id: str) -> dict[str, Any]:
        """Read without changing counters, stage, timestamps or promotion state."""
        return self._store.read(item_id)

    def read(self, item_id: str) -> dict[str, Any]:
        """Read a memory item and increment its access_count."""
        item = self._store.read(item_id)
        new_count = item.get("access_count", 0) + 1
        self._store.update(item["_path"], metadata_updates={"access_count": new_count, "stage": "retrieved"})
        self._logger.append("read", item_id, item.get("layer", "unknown"))

        # Side-effect: silently promote if eligible after access_count increment
        item["access_count"] = new_count
        self._auto_promote_if_eligible(item_id=item_id, item=item)

        return item

    # ------------------------------------------------------------------ #
    # Use                                                                   #
    # ------------------------------------------------------------------ #

    def use(self, item_id: str) -> dict[str, Any]:
        """Mark a memory item as 'in use'."""
        item = self._store.read(item_id)
        new_count = item.get("access_count", 0) + 1
        self._store.update(item["_path"], metadata_updates={"access_count": new_count, "stage": "used"})
        self._logger.append("use", item_id, item.get("layer", "unknown"))
        return item

    # ------------------------------------------------------------------ #
    # Update                                                                #
    # ------------------------------------------------------------------ #

    def update(self, item_id: str, content: str) -> None:
        """Update the content of an existing memory item."""
        item = self._store.read(item_id)
        self._store.update(item["_path"], content=content)
        # Re-index updated content
        self._fts.index_item(
            item_id=item_id,
            content=content,
            metadata={"layer": item.get("layer", "")},
        )
        self._logger.append("update", item_id, item.get("layer", "unknown"))
        self._enqueue_qmd_refresh("update")

    # ------------------------------------------------------------------ #
    # Promote                                                               #
    # ------------------------------------------------------------------ #

    def promote(
        self,
        item_id: str,
        target_layer: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        force: bool = False,
    ) -> str:
        """Promote a memory item to the next layer (or specified target_layer)."""
        item = self._store.read(item_id)
        current_layer = item.get("layer", "")

        if target_layer is None:
            target_layer = self._policy.get_next_layer(current_layer)
            if target_layer is None:
                raise PolicyViolationError(
                    f"Layer '{current_layer}' has no higher layer to promote to."
                )

        if not force:
            self._policy.validate_promote(item=item, target_layer=target_layer)
        # when force=True: skip validate_promote entirely (age/policy bypassed)

        content = item["content"]
        new_metadata = {
            k: v for k, v in item.items() if k not in ("content", "_path", "layer", "stage")
        }
        new_metadata["layer"] = target_layer
        new_metadata["stage"] = "promoted"

        self._store.write(
            layer=target_layer,
            item_id=item_id,
            content=content,
            metadata=new_metadata,
            run_id=run_id,
            session_id=session_id,
        )

        # Remove from old location
        self._store.delete(item["_path"])

        # Update FTS index
        self._fts.index_item(
            item_id=item_id,
            content=content,
            metadata={"layer": target_layer},
        )

        self._logger.append(
            "promote",
            item_id,
            target_layer,
            {"from_layer": current_layer},
        )
        self._enqueue_qmd_refresh("promote")
        content_preview = content[:_CONTENT_PREVIEW_LENGTH]
        self._hooks.fire("post-promote", {"item_id": item_id, "layer": target_layer})
        self._event_bus.emit("post-promote", {
            "item_id": item_id,
            "content_preview": content_preview,
            "from_layer": current_layer,
            "to_layer": target_layer,
        })

        # Observability: record this promotion event (async, non-blocking)
        self._obs.log_promotion(
            memory_id=item_id,
            from_layer=current_layer,
            to_layer=target_layer,
            session_id=session_id or self._session_id,
        )

        return item_id

    # ------------------------------------------------------------------ #
    # Demote                                                                #
    # ------------------------------------------------------------------ #

    def demote(
        self,
        item_id: str,
        target_layer: str,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        """Demote a memory item to a lower layer."""
        item = self._store.read(item_id)
        current_layer = item.get("layer", "")

        self._policy.validate_demote(item=item, target_layer=target_layer)

        content = item["content"]
        new_metadata = {
            k: v for k, v in item.items() if k not in ("content", "_path", "layer", "stage")
        }
        new_metadata["layer"] = target_layer
        new_metadata["stage"] = "demoted"

        self._store.write(
            layer=target_layer,
            item_id=item_id,
            content=content,
            metadata=new_metadata,
            run_id=run_id,
            session_id=session_id,
        )
        self._store.delete(item["_path"])

        self._fts.index_item(
            item_id=item_id,
            content=content,
            metadata={"layer": target_layer},
        )

        self._logger.append(
            "demote",
            item_id,
            target_layer,
            {"from_layer": current_layer},
        )
        self._enqueue_qmd_refresh("demote")
        return item_id

    # ------------------------------------------------------------------ #
    # Archive                                                               #
    # ------------------------------------------------------------------ #

    def archive(self, item_id: str) -> None:
        """Soft-delete a memory item by setting its stage to 'archived'."""
        item = self._store.read(item_id)
        self._store.update(item["_path"], metadata_updates={"stage": "archived"})
        self._logger.append("archive", item_id, item.get("layer", "unknown"))
        self._enqueue_qmd_refresh("archive")
        self._hooks.fire("post-archive", {"item_id": item_id})
        self._event_bus.emit("post-archive", {"item_id": item_id, "layer": item.get("layer", "unknown")})

    # ------------------------------------------------------------------ #
    # Forget                                                                #
    # ------------------------------------------------------------------ #

    def forget(self, item_id: str) -> None:
        """Hard-delete a memory item (requires archived stage)."""
        item = self._store.read(item_id)
        self._policy.validate_forget(item=item)

        self._store.delete(item["_path"])
        self._fts.remove(item_id)

        self._logger.append("forget", item_id, item.get("layer", "unknown"))
        self._enqueue_qmd_refresh("forget")
        self._hooks.fire("post-forget", {"item_id": item_id})
        self._event_bus.emit("post-forget", {"item_id": item_id})

    # ------------------------------------------------------------------ #
    # Delete                                                                #
    # ------------------------------------------------------------------ #

    def delete(self, item_id: str) -> None:
        """Unconditionally hard-delete a memory item by ID.

        Unlike :meth:`forget`, ``delete`` does not require the item to be in the
        ``archived`` stage first.  It is intended for AI cleanup workflows where
        a transient capture (ephemeral/session layer) should be removed
        immediately without going through the archive lifecycle step.

        The item is removed from the backing store, the FTS index, and an audit
        log entry is written.
        """
        item = self._store.read(item_id)
        layer = item.get("layer", "unknown")

        self._store.delete(item["_path"])
        self._fts.remove(item_id)

        self._logger.append("delete", item_id, layer)
        self._enqueue_qmd_refresh("delete")
        self._hooks.fire("post-delete", {"item_id": item_id})
        self._event_bus.emit("post-delete", {"item_id": item_id, "layer": layer})

    # ------------------------------------------------------------------ #
    # Consolidate                                                           #
    # ------------------------------------------------------------------ #

    def consolidate(self, *, run_distill: bool = True) -> int:
        """Sweep ALL memories across all layers and promote eligible ones.

        This is the engine behind `mnemos consolidate`. It evaluates every
        memory item against policy.yaml thresholds and promotes those that
        qualify. Promotion decisions are fully owned by mnemos — AI has no role.

        ``run_distill`` controls only the end-of-sweep automatic distillation
        hook. Manual ``mnemos consolidate`` keeps the default ``True`` behavior;
        background auto-promotion disables it so due distillation is drained by
        the dedicated maintenance path instead of every promotion sweep.

        Returns the total number of items promoted.
        """
        from core.layers import LAYER_STATIC_PATHS

        promoted_count = 0

        # Collect all layers known to the store (static + dynamic)
        static_layers = list(LAYER_STATIC_PATHS.keys())
        dynamic_layers = ["ephemeral", "working", "session"]
        all_layers = static_layers + [l for l in dynamic_layers if l not in static_layers]

        for layer in all_layers:
            for item in self._store.iter_layer_items(layer):
                try:
                    item_id = item.get("id") or Path(item["_path"]).stem
                    if not self._policy.check_promotion_eligible(item):
                        continue
                    next_layer = self._policy.get_next_layer(item.get("layer", ""))
                    if next_layer is None:
                        continue
                    self.promote(item_id=item_id, target_layer=next_layer)
                    promoted_count += 1
                except Exception:
                    # Skip items that fail — consolidate is best-effort
                    continue

        # End-of-sweep automatic distillation (#87). Manual consolidate fires
        # unconditionally when enabled; background promotion opts out and lets
        # ``run_due_auto_distill`` drain only due work.
        try:
            if run_distill and self._distill_enabled:
                from core.distill import run_auto_distill

                report = run_auto_distill(self)
                self._reset_distill_counter()
                self._obs.log_auto_distill(
                    success=True,
                    trigger="consolidate",
                    interval=self._distill_interval,
                    domains_applied=report["domains"]["applied"],
                    policies_applied=report["policies"]["applied"],
                )
        except Exception as exc:  # noqa: BLE001
            try:
                self._obs.log_auto_distill(
                    success=False,
                    error=str(exc),
                    trigger="consolidate",
                    interval=self._distill_interval,
                )
            except Exception:  # pragma: no cover - observability is best-effort
                pass

        return promoted_count

    # ------------------------------------------------------------------ #
    # List                                                                  #
    # ------------------------------------------------------------------ #

    def list_all(
        self,
        layers: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all memory items, optionally filtered by layer(s) and capped by limit."""
        from core.layers import LAYER_STATIC_PATHS

        static_layers = list(LAYER_STATIC_PATHS.keys())
        dynamic_layers = ["ephemeral", "working", "session"]
        all_layers = static_layers + dynamic_layers
        if layers:
            all_layers = [l for l in all_layers if l in layers]

        results: list[dict[str, Any]] = []

        for layer in all_layers:
            if limit is not None and len(results) >= limit:
                break

            for item in self._store.iter_layer_items(layer):
                if limit is not None and len(results) >= limit:
                    break
                try:
                    results.append({
                        "item_id": item.get("id") or Path(item["_path"]).stem,
                        "layer": item.get("layer", layer),
                        "content": item.get("content", ""),
                        "tags": item.get("tags", []),
                        "created_at": item.get("created_at"),
                    })
                except Exception:
                    continue

        return results

    # ------------------------------------------------------------------ #
    # Log                                                                   #
    # ------------------------------------------------------------------ #

    def log(self, operation: str, item_id: str, layer: str, metadata: dict[str, Any] | None = None) -> None:
        """Manually append an entry to the audit log."""
        self._logger.append(operation=operation, item_id=item_id, layer=layer, metadata=metadata)

    # ------------------------------------------------------------------ #
    # Garbage collection                                                    #
    # ------------------------------------------------------------------ #

    def gc(
        self,
        dry_run: bool = False,
        layers: list[str] | None = None,
        staleness_hours: float | None = None,
        gc_threshold: float | None = None,
        limit: int | None = None,
    ) -> "GCReport":  # noqa: F821 – forward ref resolved at runtime
        """Run G1GC-style garbage collection and return a :class:`~core.gc.GCReport`.

        This is the programmatic API counterpart of the ``mnemos gc`` CLI
        command.  Memories whose composite garbage score exceeds *gc_threshold*
        are soft-archived (``stage=archived``).  No memory is ever
        hard-deleted.

        Parameters
        ----------
        dry_run:
            When ``True``, compute scores and identify candidates without
            modifying any files.
        layers:
            Restrict GC to these layer names.  ``None`` means all layers.
        staleness_hours:
            Age threshold for staleness scoring (default: 24 h).
        gc_threshold:
            Minimum garbage score [0.0–1.0] to collect a memory
            (default: 0.7).
        limit:
            Maximum number of memories to archive per run (default: 100).

        Returns
        -------
        GCReport
            Detailed report including per-item scores and reasons.
        """
        from core.gc import (
            GarbageCollector,
            DEFAULT_STALENESS_HOURS,
            DEFAULT_GC_THRESHOLD,
            DEFAULT_LIMIT,
            GCReport,
        )

        collector = GarbageCollector(
            repo_root=self._root,
            staleness_hours=staleness_hours if staleness_hours is not None else DEFAULT_STALENESS_HOURS,
            gc_threshold=gc_threshold if gc_threshold is not None else DEFAULT_GC_THRESHOLD,
            limit=limit if limit is not None else DEFAULT_LIMIT,
            layers=layers,
        )
        report = collector.run(dry_run=dry_run)

        # Audit log: record which items were archived by GC
        if not dry_run:
            for item_info in report.archived_items:
                self._logger.append(
                    operation="gc_archive",
                    item_id=item_info["item_id"],
                    layer=item_info["layer"],
                    metadata={
                        "garbage_score": item_info["garbage_score"],
                        "reason": item_info["reason"],
                    },
                )

        # Observability: record the GC run (async, non-blocking)
        self._obs.log_gc(
            archived_count=len(report.archived_items),
            dry_run=dry_run,
            layers=layers,
            session_id=self._session_id,
        )

        return report
