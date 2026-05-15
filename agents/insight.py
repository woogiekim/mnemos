"""Insight Extractor Agent — uses claude-haiku-4-5 to judge conversation turns."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Quality categories that are worth capturing
_QUALITY_CRITERIA = """\
Capture the insight if it contains ANY of:
  - decision + reason  (why a design/implementation choice was made)
  - bug root cause     (what caused a defect and why)
  - constraint/invariant (a rule that must always hold)
  - pattern/convention (a reusable approach specific to this codebase/project)
  - pitfall warning    (something that will break or mislead future readers)

Do NOT capture:
  - routine question-and-answer exchanges
  - file reads with no interpretation
  - mechanical edits (rename, reformat) with no rationale
  - status updates ("done", "ok", "looks good")
"""

_SYSTEM_PROMPT = """\
You are a memory curator for a software project. \
You read a conversation turn and decide whether it contains \
an insight worth persisting to long-term memory.

""" + _QUALITY_CRITERIA + """

If the turn is worth capturing, respond with a JSON array of insight objects, \
each with the following fields:
  { "content": "<one concise sentence capturing the insight>",
    "category": "<decision|bug_root_cause|constraint|pattern|pitfall>" }

If the turn contains NOTHING worth capturing, respond with an empty JSON array: []

Respond ONLY with valid JSON. No markdown, no explanation outside the JSON.
"""


class InsightExtractor:
    """Calls claude-haiku-4-5 to extract worth-capturing insights from a conversation turn."""

    MODEL = "claude-haiku-4-5"

    def extract(self, turn_text: str) -> list[dict[str, str]]:
        """
        Judge whether *turn_text* contains worth-capturing insights.

        Args:
            turn_text: Raw text of one conversation turn (assistant message,
                       tool result, or combined exchange).

        Returns:
            List of dicts, each with ``"content"`` and ``"category"`` keys.
            Empty list when nothing is worth capturing.

        Raises:
            RuntimeError: If the Anthropic API call fails.
        """
        try:
            import anthropic  # noqa: PLC0415  (deferred import — optional dep)
        except ImportError as exc:
            raise RuntimeError(
                "anthropic SDK not installed. "
                "Run: pip install anthropic  or  pipx inject mnemos anthropic"
            ) from exc

        client = anthropic.Anthropic()

        try:
            message = client.messages.create(
                model=self.MODEL,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": f"<conversation_turn>\n{turn_text}\n</conversation_turn>",
                    }
                ],
            )
        except anthropic.APIError as exc:
            raise RuntimeError(f"Anthropic API error: {exc}") from exc

        raw = message.content[0].text.strip() if message.content else "[]"
        try:
            insights = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("LLM returned non-JSON: %r — treating as empty", raw[:200])
            return []

        if not isinstance(insights, list):
            logger.warning("LLM returned non-list JSON — treating as empty")
            return []

        valid: list[dict[str, str]] = []
        for item in insights:
            if isinstance(item, dict) and "content" in item:
                valid.append(
                    {
                        "content": str(item["content"]),
                        "category": str(item.get("category", "general")),
                    }
                )
        return valid

    # ------------------------------------------------------------------
    # Subprocess helper (used by the CLI command)
    # ------------------------------------------------------------------

    def capture_via_subprocess(
        self,
        insight: dict[str, str],
        session_id: str | None = None,
        dry_run: bool = False,
    ) -> bool:
        """
        Call ``mnemos memory-capture --layer session`` for a single insight.

        Args:
            insight: Dict with ``"content"`` and ``"category"`` keys.
            session_id: Optional Claude Code session ID for scoping.
            dry_run: When True, print instead of calling memory-capture.

        Returns:
            True on success, False on failure.
        """
        content = insight["content"]
        category = insight.get("category", "general")
        tags = ["insight", "auto-extracted", category]

        if dry_run:
            print(f"[dry-run] would capture: {content!r}  (category={category})")
            return True

        cmd = [
            sys.executable, "-m", "core.cli",
            "memory-capture",
            "--layer", "session",
            "--content", content,
        ]
        for tag in tags:
            cmd += ["--tag", tag]
        if session_id:
            cmd += ["--session-id", session_id]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                logger.warning(
                    "memory-capture failed (exit %d): %s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            logger.warning("memory-capture timed out")
            return False
        except Exception as exc:
            logger.warning("memory-capture subprocess error: %s", exc)
            return False
