"""Memory Gateway — single entry point for all memory operations."""
from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path
from typing import Any

from mnemos.policy import PolicyEngine, PolicyViolationError
from mnemos.store import MemoryStore
from mnemos.log import AuditLogger
from mnemos.hooks import HookDispatcher
from mnemos.fts import FTSIndex
from mnemos.search import SearchMiddleware


DEFAULT_QUALITY_SCORE = 0.8


class MemoryGateway:
    """
    Single entry point for all memory lifecycle operations.

    All mutations are validated by PolicyEngine, persisted by MemoryStore,
    logged by AuditLogger, and trigger HookDispatcher events.
    """

    def __init__(self, repo_root: str | None = None) -> None:
        self._root = repo_root or os.environ.get("MNEMOS_REPO_ROOT", ".")
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

    # ------------------------------------------------------------------ #
    # Capture                                                               #
    # ------------------------------------------------------------------ #

    def capture(
        self,
        layer: str,
        content: str,
        item_id: str | None = None,
        tags: list[str] | None = None,
        quality_score: float = DEFAULT_QUALITY_SCORE,
        run_id: str | None = None,
        session_id: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> str:
        """Capture a new memory item into the target layer."""
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
        """Search across memory layers."""
        return self._search.search(query=query, layers=layers, limit=limit)

    # ------------------------------------------------------------------ #
    # Read                                                                  #
    # ------------------------------------------------------------------ #

    def read(self, item_id: str) -> dict[str, Any]:
        """Read a memory item and increment its access_count."""
        item = self._store.read(item_id)
        new_count = item.get("access_count", 0) + 1
        self._store.update(item["_path"], metadata_updates={"access_count": new_count, "stage": "retrieved"})
        self._logger.append("read", item_id, item.get("layer", "unknown"))
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

        new_path = self._store.write(
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
    # Log                                                                   #
    # ------------------------------------------------------------------ #

    def log(self, operation: str, item_id: str, layer: str, metadata: dict[str, Any] | None = None) -> None:
        """Manually append an entry to the audit log."""
        self._logger.append(operation=operation, item_id=item_id, layer=layer, metadata=metadata)
