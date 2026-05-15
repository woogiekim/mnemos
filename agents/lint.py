"""Lint Agent — validates YAML front-matter and Markdown formatting."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

if TYPE_CHECKING:
    from core.gateway import MemoryGateway

logger = logging.getLogger(__name__)

REQUIRED_FRONT_MATTER_FIELDS = ["id", "layer", "stage", "created_at"]
WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")


class LintAgent:
    """
    Validates all wiki pages for:
    - Required YAML front-matter fields
    - Broken [[wikilink]] references
    - Orphan pages (no backlinks or wikilinks)
    """

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(self) -> dict[str, Any]:
        """
        Lint all wiki pages and write a report.

        Returns:
            Dict with 'errors', 'warnings', and 'report_path'.
        """
        wiki_root = Path(self._gw._root) / "wiki"
        reports_dir = Path(self._gw._root) / ".agent" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)

        errors: list[str] = []
        warnings: list[str] = []

        all_page_ids: set[str] = set()
        page_data: dict[str, dict[str, Any]] = {}

        # First pass: collect all page IDs and validate front-matter
        for md_file in wiki_root.rglob("*.md"):
            if md_file.name in ("log.md", "index.md"):
                continue
            page_id = md_file.stem
            all_page_ids.add(page_id)

            try:
                post = frontmatter.load(str(md_file))
                meta = dict(post.metadata)
                page_data[page_id] = {"meta": meta, "content": post.content, "path": md_file}

                # Check required fields
                for field in REQUIRED_FRONT_MATTER_FIELDS:
                    if field not in meta:
                        errors.append(
                            f"{md_file.name}: missing required front-matter field '{field}'"
                        )
            except Exception as exc:
                errors.append(f"{md_file.name}: invalid YAML front-matter — {exc}")

        # Second pass: check for broken wikilinks and orphan pages
        referenced_ids: set[str] = set()
        for page_id, data in page_data.items():
            content = data["content"]
            refs = WIKILINK_PATTERN.findall(content)
            for ref in refs:
                ref_id = ref.strip()
                referenced_ids.add(ref_id)
                if ref_id not in all_page_ids:
                    warnings.append(
                        f"{page_id}: broken wikilink [[{ref_id}]] — target page not found"
                    )

        # Orphan detection
        for page_id in all_page_ids:
            if page_id not in referenced_ids:
                warnings.append(f"{page_id}: orphan page — not referenced by any other page")

        # Write report
        report_path = reports_dir / "lint.md"
        self._write_report(report_path, errors, warnings)

        return {
            "errors": errors,
            "warnings": warnings,
            "report_path": str(report_path),
            "pages_checked": len(all_page_ids),
        }

    def _write_report(
        self,
        report_path: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Write lint report."""
        lines = ["# Lint Report\n"]
        if errors:
            lines.append(f"## Errors ({len(errors)})\n")
            for e in errors:
                lines.append(f"- ERROR: {e}")
        else:
            lines.append("## Errors\nNone.\n")

        if warnings:
            lines.append(f"\n## Warnings ({len(warnings)})\n")
            for w in warnings:
                lines.append(f"- WARN: {w}")
        else:
            lines.append("\n## Warnings\nNone.\n")

        report_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(
            "Lint report written to %s (%d errors, %d warnings)",
            report_path,
            len(errors),
            len(warnings),
        )
