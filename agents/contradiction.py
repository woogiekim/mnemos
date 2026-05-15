"""Contradiction Agent — detects conflicting claims in wiki/claims/."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

if TYPE_CHECKING:
    from mnemos.gateway import MemoryGateway

logger = logging.getLogger(__name__)


class ContradictionAgent:
    """
    Reads wiki/claims/ and detects potentially conflicting claims.

    Uses keyword/semantic similarity heuristics to identify contradictions
    and writes a report to .agent/reports/contradictions.md.
    """

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(self) -> dict[str, Any]:
        """
        Detect conflicting claims and write a report.

        Returns:
            Dict with 'contradictions' list and 'report_path' string.
        """
        claims_dir = Path(self._gw._root) / "wiki" / "claims"
        reports_dir = Path(self._gw._root) / ".agent" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        claims = self._load_claims(claims_dir)
        contradictions = self._detect_contradictions(claims)

        report_path = reports_dir / "contradictions.md"
        self._write_report(report_path, contradictions)

        return {
            "contradictions": contradictions,
            "report_path": str(report_path),
            "total_claims": len(claims),
        }

    def _load_claims(self, claims_dir: Path) -> list[dict[str, Any]]:
        """Load all claim files from wiki/claims/."""
        claims = []
        if not claims_dir.exists():
            return claims
        for md_file in claims_dir.glob("*.md"):
            try:
                post = frontmatter.load(str(md_file))
                claim = dict(post.metadata)
                claim["content"] = post.content
                claim["_path"] = str(md_file)
                claim["_id"] = md_file.stem
                claims.append(claim)
            except Exception as exc:
                logger.warning("Could not parse claim %s: %s", md_file, exc)
        return claims

    def _detect_contradictions(
        self, claims: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Detect contradictions using simple keyword heuristics.

        Looks for claim pairs where one contains negation keywords
        relative to the other.
        """
        contradictions = []
        negation_words = {"not", "no", "never", "false", "incorrect", "wrong", "deny", "denies"}

        for i, claim_a in enumerate(claims):
            for j, claim_b in enumerate(claims):
                if j <= i:
                    continue
                content_a = claim_a["content"].lower()
                content_b = claim_b["content"].lower()

                # Simple heuristic: one claim's words appear in other with negation
                words_a = set(re.findall(r"\w+", content_a))
                words_b = set(re.findall(r"\w+", content_b))
                common_words = words_a & words_b - {"the", "a", "an", "is", "are", "was"}

                if common_words and (
                    bool(negation_words & words_a) != bool(negation_words & words_b)
                ):
                    contradictions.append(
                        {
                            "claim_a": claim_a["_id"],
                            "claim_b": claim_b["_id"],
                            "common_words": list(common_words)[:10],
                            "confidence": "low",
                        }
                    )

        return contradictions

    def _write_report(
        self, report_path: Path, contradictions: list[dict[str, Any]]
    ) -> None:
        """Write contradiction report to .agent/reports/contradictions.md."""
        lines = ["# Contradiction Report\n"]
        if not contradictions:
            lines.append("No contradictions detected.\n")
        else:
            lines.append(f"Found {len(contradictions)} potential contradiction(s):\n")
            for c in contradictions:
                lines.append(
                    f"- **{c['claim_a']}** vs **{c['claim_b']}** "
                    f"(confidence: {c['confidence']}, "
                    f"common terms: {', '.join(c['common_words'])})\n"
                )

        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Contradiction report written to %s", report_path)
