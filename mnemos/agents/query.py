"""Query Agent — answers questions using memory-search + memory-read."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mnemos.gateway import MemoryGateway

logger = logging.getLogger(__name__)


class QueryAgent:
    """Answers natural language questions by searching and reading memories."""

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(
        self,
        question: str,
        run_id: str = "default",
        search_limit: int = 5,
    ) -> dict[str, Any]:
        """
        Answer a question using memory-search and memory-read.

        Args:
            question: The question to answer.
            run_id: Run ID for scoping ephemeral memory.
            search_limit: Maximum number of memories to retrieve.

        Returns:
            Dict with 'question', 'answer', 'sources', and 'item_ids'.
        """
        # Search for relevant memories
        results = self._gw.search(query=question, limit=search_limit)

        if not results:
            return {
                "question": question,
                "answer": f"No memories found for: '{question}'",
                "sources": [],
                "item_ids": [],
            }

        # Read and collect relevant content
        sources = []
        item_ids = []
        answer_parts = []

        for result in results:
            item_id = result["item_id"]
            item_ids.append(item_id)
            try:
                item = self._gw.use(item_id=item_id)
                content = item.get("content", result.get("content", ""))
                sources.append(
                    {
                        "item_id": item_id,
                        "layer": item.get("layer", "unknown"),
                        "snippet": content[:300],
                    }
                )
                answer_parts.append(content[:500])
            except Exception as exc:
                logger.warning("Could not read item %s: %s", item_id, exc)
                sources.append({"item_id": item_id, "layer": "unknown", "snippet": result.get("content", "")[:300]})

        # Build answer from collected content
        answer = "\n\n---\n\n".join(answer_parts) if answer_parts else "No readable content found."

        return {
            "question": question,
            "answer": answer,
            "sources": sources,
            "item_ids": item_ids,
        }
