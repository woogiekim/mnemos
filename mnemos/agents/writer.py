"""Writer Agent — generates or rewrites wiki entries from captured memories."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mnemos.gateway import MemoryGateway

logger = logging.getLogger(__name__)


class WriterAgent:
    """Searches memories on a topic and generates a wiki entry."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(
        self,
        topic: str,
        run_id: str = "default",
        target_layer: str = "project",
        search_limit: int = 10,
    ) -> str:
        """
        Search memories for a topic and generate or update a wiki entry.

        Args:
            topic: Topic to write about.
            run_id: Run ID for scoping.
            target_layer: Layer to write the wiki entry to.
            search_limit: Maximum number of memories to retrieve.

        Returns:
            Item ID of the created or updated wiki entry.
        """
        results = self._gw.search(query=topic, limit=search_limit)

        # Build wiki entry from search results
        sections = [f"# {topic}\n"]
        if results:
            sections.append("## Related Memories\n")
            for r in results:
                item_id = r["item_id"]
                snippet = r["content"][:200].strip()
                sections.append(f"- `{item_id}`: {snippet}")
        else:
            sections.append(f"No memories found for topic: {topic}")

        content = "\n".join(sections)

        # Check if a wiki entry for this topic already exists
        existing = self._gw.search(query=f"# {topic}", layers=[target_layer], limit=1)
        if existing:
            existing_id = existing[0]["item_id"]
            self._gw.update(item_id=existing_id, content=content)
            logger.info("Writer: updated wiki entry %s for topic '%s'", existing_id, topic)
            return existing_id
        else:
            item_id = self._gw.capture(
                layer=target_layer,
                content=content,
                tags=["wiki", "generated", topic],
                run_id=run_id,
            )
            logger.info("Writer: created wiki entry %s for topic '%s'", item_id, topic)
            return item_id
