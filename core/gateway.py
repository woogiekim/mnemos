"""Memory Gateway — single entry point for all memory operations."""
from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path
from typing import Any

from core.policy import PolicyEngine, PolicyViolationError
from core.store import MemoryStore
from core.log import AuditLogger
from core.hooks import HookDispatcher
from core.fts import FTSIndex
from core.search import SearchMiddleware


DEFAULT_QUALITY_SCORE = 0.8


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
        state_dir = Path(self._root) / ".agent" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        fts_db = str(state_dir / "fts.db")
        self._fts = FTSIndex(db_path=fts_db)
        self._search = SearchMiddleware(
            repo_root=self._root,
            fts_index=self._fts,
        )
        # Auto-generated IDs scoped to this gateway instance (i.e. this process)
        self._run_id: str = str(uuid.uuid4())
        self._session_id: str = str(uuid.uuid4())

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
        It never raises and never produces output — promotion is entirely
        transparent to the caller.
        """
        try:
            if not self._policy.check_promotion_eligible(item):
                return
            next_layer = self._policy.get_next_layer(item.get("layer", ""))
            if next_layer is None:
                return
            self.promote(item_id=item_id, target_layer=next_layer)
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
    ) -> str:
        """Capture a new memory item into the target layer.

        When *layer* is omitted it defaults to ``"ephemeral"``.  The
        *run_id* and *session_id* are filled from this gateway instance's
        auto-generated values when not supplied by the caller, ensuring
        that ephemeral items always land in a deterministic path for the
        duration of the process.
        """
        if layer is None:
            layer = _DEFAULT_LAYER

        # Fill dynamic IDs from gateway defaults when not explicitly provided
        if run_id is None:
            run_id = self._run_id
        if session_id is None:
            session_id = self._session_id

        self._policy.validate_capture(layer=layer, item={"content": content})

        item_id = item_id or str(uuid.uuid4())
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
        self._hooks.fire("post-capture", {"item_id": item_id, "layer": layer})

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
        self._hooks.fire("post-promote", {"item_id": item_id, "layer": target_layer})

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
            # For dynamic layers that need run_id/session_id, scan the
            # underlying directories directly to find all items.
            if layer in ("ephemeral", "working"):
                agent_runs = Path(self._root) / ".agent" / "runs"
                if not agent_runs.exists():
                    continue
                run_dirs = [d for d in agent_runs.iterdir() if d.is_dir()]
                sub = "scratch" if layer == "ephemeral" else "working"
                paths = []
                for rd in run_dirs:
                    layer_dir = rd / sub
                    if layer_dir.exists():
                        paths.extend(layer_dir.glob("*.md"))
            elif layer == "session":
                agent_sessions = Path(self._root) / ".agent" / "sessions"
                if not agent_sessions.exists():
                    continue
                paths = list(agent_sessions.rglob("*.md"))
            else:
                paths = list(self._store.list_layer(layer))

            for item_path in paths:
                try:
                    item = self._store._parse_file(item_path)
                    item_id = item.get("id") or item_path.stem
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

            paths: list[Path] = []
            if layer == "ephemeral":
                agent_runs = Path(self._root) / ".agent" / "runs"
                if agent_runs.exists():
                    for rd in agent_runs.iterdir():
                        if rd.is_dir():
                            scratch = rd / "scratch"
                            if scratch.exists():
                                paths.extend(scratch.glob("*.md"))
            elif layer == "working":
                agent_runs = Path(self._root) / ".agent" / "runs"
                if agent_runs.exists():
                    for rd in agent_runs.iterdir():
                        if rd.is_dir():
                            working_dir = rd / "working"
                            if working_dir.exists():
                                paths.extend(working_dir.glob("*.md"))
            elif layer == "session":
                agent_sessions = Path(self._root) / ".agent" / "sessions"
                if agent_sessions.exists():
                    paths = list(agent_sessions.rglob("*.md"))
            else:
                paths = list(self._store.list_layer(layer))

            for item_path in paths:
                if limit is not None and len(results) >= limit:
                    break
                try:
                    item = self._store._parse_file(item_path)
                    results.append({
                        "item_id": item.get("id") or item_path.stem,
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
