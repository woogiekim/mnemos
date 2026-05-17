"""Memory Gateway — single entry point for all memory operations."""
from __future__ import annotations

import datetime
import hashlib
import os
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from core.policy import PolicyEngine, PolicyViolationError
from core.store import MemoryStore
from core.log import AuditLogger
from core.hooks import HookDispatcher
from core.events import EventBus
from core.fts import FTSIndex
from core.observability import ObservabilityLogger
from core.search import SearchMiddleware


DEFAULT_QUALITY_SCORE = 0.8
_CONTENT_PREVIEW_LENGTH = 60


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


def _resolve_repo_root() -> Path:
    """Locate the mnemos repo root by checking, in order:

    1. The ``MNEMOS_REPO_ROOT`` environment variable (validated).
    2. Walking up from the current working directory.
    3. Walking up from this source file's location.

    Raises ``FileNotFoundError`` with a human-readable message if none of
    the strategies succeed.
    """
    # 1. Check MNEMOS_REPO_ROOT env var
    env_val = os.environ.get("MNEMOS_REPO_ROOT")
    if env_val:
        p = Path(env_val).expanduser().resolve()
        if (p / "wiki" / "policy.yaml").exists():
            return p
        # env var is set but does not point at a valid repo — fail immediately
        raise FileNotFoundError(
            f"MNEMOS_REPO_ROOT={env_val!r} does not contain wiki/policy.yaml"
        )

    # 2. Walk up from CWD
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "wiki" / "policy.yaml").exists():
            return parent

    # 3. Walk up from __file__
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "wiki" / "policy.yaml").exists():
            return parent

    raise FileNotFoundError(
        "Cannot find mnemos repo root. "
        "Set the MNEMOS_REPO_ROOT environment variable to the repo path."
    )


_DEFAULT_LAYER = "ephemeral"


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
        policy_path = str(Path(self._root) / "wiki" / "policy.yaml")
        self._policy = PolicyEngine(policy_path=policy_path)
        self._store = MemoryStore(repo_root=self._root)
        self._logger = AuditLogger(repo_root=self._root)
        self._hooks = HookDispatcher(repo_root=self._root)
        self._event_bus: EventBus = EventBus()
        self._obs = ObservabilityLogger(repo_root=self._root)
        state_dir = Path(self._root) / ".agent" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        fts_db = str(state_dir / "fts.db")
        self._fts = FTSIndex(db_path=fts_db)
        self._search = SearchMiddleware(
            repo_root=self._root,
            fts_index=self._fts,
            store=self._store,
        )
        # Auto-generated IDs scoped to this gateway instance (i.e. this process)
        self._run_id: str = str(uuid.uuid4())
        self._session_id: str = str(uuid.uuid4())
        # On-write dedup registry: maps (layer, content_hash) → item_id.
        # Keyed by layer so cross-layer promotion is never blocked.
        self._capture_dedup: dict[tuple[str, str], str] = {}

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
    # Capture                                                               #
    # ------------------------------------------------------------------ #

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
    ) -> str | None:
        """Capture a new memory item into the target layer.

        When *layer* is omitted it defaults to ``"ephemeral"``.  The
        *run_id* and *session_id* are filled from this gateway instance's
        auto-generated values when not supplied by the caller, ensuring
        that ephemeral items always land in a deterministic path for the
        duration of the process.

        On-write dedup
        --------------
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

        **Cross-layer promotion is not blocked.**  The dedup key is
        layer-scoped, so capturing the same content into ``session`` after
        it has already been promoted to ``global`` is allowed and returns a
        new item_id.  This matches the invariant stated in the requirements:
        ``dedup key is (layer, hash) — promotion across layers is NOT blocked
        by dedup``.
        """
        if layer is None:
            layer = _DEFAULT_LAYER

        # Fill dynamic IDs from gateway defaults when not explicitly provided
        if run_id is None:
            run_id = self._run_id
        if session_id is None:
            session_id = self._session_id

        # ------------------------------------------------------------------
        # On-write dedup check (Issue #4 anti-regression)
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
            return None

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
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        self._store.write(
            layer=layer,
            item_id=item_id,
            content=content,
            metadata=metadata,
            run_id=run_id,
            session_id=session_id,
        )

        self._fts.index_item(
            item_id=item_id,
            content=content,
            metadata={"layer": layer, "tags": tags or []},
        )

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

    # ------------------------------------------------------------------ #
    # Search                                                                #
    # ------------------------------------------------------------------ #

    def search(
        self,
        query: str,
        layers: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search across memory layers.

        After returning results, access_count is incremented for each hit and
        each hit is checked for silent auto-promotion.
        """
        results = self._search.search(query=query, layers=layers, limit=limit)

        # Side-effect: increment access_count and auto-promote eligible results
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
                item["access_count"] = new_count
                self._auto_promote_if_eligible(item_id=result_item_id, item=item)
            except Exception:
                pass

        # Observability: record this search event (async, non-blocking)
        self._obs.log_search(
            keywords=[query],
            results=results,
            session_id=self._session_id,
        )

        return results

    # ------------------------------------------------------------------ #
    # Read                                                                  #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Promote                                                               #
    # ------------------------------------------------------------------ #

    def promote(
        self,
        item_id: str,
        target_layer: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
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

        self._policy.validate_promote(item=item, target_layer=target_layer)

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
        return item_id

    # ------------------------------------------------------------------ #
    # Archive                                                               #
    # ------------------------------------------------------------------ #

    def archive(self, item_id: str) -> None:
        """Soft-delete a memory item by setting its stage to 'archived'."""
        item = self._store.read(item_id)
        self._store.update(item["_path"], metadata_updates={"stage": "archived"})
        self._logger.append("archive", item_id, item.get("layer", "unknown"))
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
        self._hooks.fire("post-forget", {"item_id": item_id})
        self._event_bus.emit("post-forget", {"item_id": item_id})

    # ------------------------------------------------------------------ #
    # Consolidate                                                           #
    # ------------------------------------------------------------------ #

    def consolidate(self) -> int:
        """Sweep ALL memories across all layers and promote eligible ones.

        This is the engine behind `mnemos consolidate`. It evaluates every
        memory item against policy.yaml thresholds and promotes those that
        qualify. Promotion decisions are fully owned by mnemos — AI has no role.

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
