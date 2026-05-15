"""Linker Agent — detects cross-references between wiki pages and adds backlinks."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

if TYPE_CHECKING:
    from mnemos.gateway import MemoryGateway

logger = logging.getLogger(__name__)

WIKILINK_PATTERN = re.compile(r"\[\[([^\[\]]+)\]\]")


class LinkerAgent:
    """
    Scans wiki pages for [[wikilink]] references and adds backlinks.

    For each page that contains [[OtherPage]] references, adds a "Backlinks"
    section to the referenced page listing the referencing page.
    """

    def __init__(self, gateway: "MemoryGateway") -> None:
        self._gw = gateway

    def run(self) -> dict[str, list[str]]:
        """
        Scan all wiki pages and detect cross-references.

        Returns:
            Dict mapping referenced item IDs to lists of referencing item IDs.
        """
        # Build index of all wiki pages
        wiki_root = Path(self._gw._root) / "wiki"
        all_pages: dict[str, Path] = {}

        for md_file in wiki_root.rglob("*.md"):
            if md_file.name in ("log.md", "index.md"):
                continue
            all_pages[md_file.stem] = md_file

        # Detect references
        backlinks: dict[str, list[str]] = {}
        for page_id, page_path in all_pages.items():
            try:
                text = page_path.read_text(encoding="utf-8")
                refs = WIKILINK_PATTERN.findall(text)
                for ref in refs:
                    ref_id = ref.strip()
                    if ref_id != page_id:
                        backlinks.setdefault(ref_id, [])
                        if page_id not in backlinks[ref_id]:
                            backlinks[ref_id].append(page_id)
            except OSError as exc:
                logger.warning("Could not read %s: %s", page_path, exc)

        # Write backlinks into referenced pages
        for ref_id, referencing_ids in backlinks.items():
            if ref_id in all_pages:
                self._write_backlinks(all_pages[ref_id], referencing_ids)

        return backlinks

    def _write_backlinks(self, page_path: Path, referencing_ids: list[str]) -> None:
        """Append or update the Backlinks section of a wiki page."""
        try:
            post = frontmatter.load(str(page_path))
            content = post.content

            # Remove existing backlinks section
            content = re.sub(
                r"\n## Backlinks\n.*$", "", content, flags=re.DOTALL
            ).rstrip()

            # Add new backlinks section
            backlink_lines = "\n".join(f"- [[{rid}]]" for rid in referencing_ids)
            content += f"\n\n## Backlinks\n{backlink_lines}\n"

            post.content = content
            with page_path.open("w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            logger.debug("Added backlinks to %s: %s", page_path.stem, referencing_ids)
        except OSError as exc:
            logger.warning("Could not write backlinks to %s: %s", page_path, exc)
