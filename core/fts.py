"""SQLite FTS5 index for full-text search of memory items."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from core.korean import expand_query

# FTS5 special characters that need quoting when used in a bare query term.
# Hyphen (-) is the most common trigger: "agent-crew" is parsed as
# "agent" MINUS "crew" rather than a phrase, causing an OperationalError.
_FTS5_SPECIAL_RE = re.compile(r'[-+*^":()\[\]{}|&!]')

# Matches the leading YAML front-matter block at the start of a string:
#   ---\n...\n---\n
# The content after the closing delimiter (and the trailing newline) is
# what we want to keep; the front-matter itself is noise in FTS snippets.
_FRONTMATTER_RE = re.compile(r'^---\s*\n.*?\n---\s*\n', re.DOTALL)


def _strip_frontmatter(content: str) -> str:
    """Remove the leading YAML front-matter block from *content*, if present.

    Returns the body text that follows the closing ``---`` delimiter, stripped
    of leading/trailing whitespace.  When no front-matter block is detected the
    original string is returned unchanged.

    Examples
    --------
    >>> _strip_frontmatter("---\\nkey: val\\n---\\nBody text here.")
    'Body text here.'
    >>> _strip_frontmatter("No frontmatter here.")
    'No frontmatter here.'
    """
    stripped = _FRONTMATTER_RE.sub("", content, count=1)
    return stripped.strip() if stripped != content else content


# Characters used as separators in compound technical terms that should be
# normalised to spaces so FTS5 can match the component tokens.  Colons and
# hyphens are the two most common: ``crew:run`` and ``agent-crew``.
_COMPOUND_SEPARATOR_RE = re.compile(r'[:\-]')


def _normalise_compound_terms(query: str) -> str:
    """Replace colons and hyphens with spaces to normalise compound technical terms.

    Converts terms like ``crew:run`` and ``agent-crew`` into ``crew run`` and
    ``agent crew`` respectively, so that FTS5 can match documents containing
    those component tokens even when the full compound form is not present
    verbatim in the index.

    Examples
    --------
    >>> _normalise_compound_terms("crew:run")
    'crew run'
    >>> _normalise_compound_terms("agent-crew")
    'agent crew'
    >>> _normalise_compound_terms("fix agent-crew and crew:run issues")
    'fix agent crew and crew run issues'
    """
    return _COMPOUND_SEPARATOR_RE.sub(" ", query)


def _sanitise_fts_variant(variant: str) -> str:
    """Wrap tokens containing FTS5 special characters in double quotes.

    FTS5 treats ``-``, ``+``, ``*``, ``^``, etc. as query operators when they
    appear *between* tokens (or as a prefix/suffix).  A term like ``agent-crew``
    is parsed as ``agent MINUS crew``, which raises ``OperationalError: no such
    column: crew``.

    Wrapping each token in double quotes makes FTS5 treat it as a literal
    phrase to match verbatim.

    Only tokens that contain at least one special character are quoted;
    plain alphanumeric tokens (and Korean tokens) are left as-is to preserve
    normal FTS5 ranking behaviour.
    """
    tokens = variant.split()
    sanitised: list[str] = []
    for tok in tokens:
        if _FTS5_SPECIAL_RE.search(tok):
            # Escape any embedded double-quotes, then wrap
            escaped = tok.replace('"', '""')
            sanitised.append(f'"{escaped}"')
        else:
            sanitised.append(tok)
    return " ".join(sanitised)


class FTSIndex:
    """Manages a SQLite FTS5 full-text search index."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create FTS5 table if it doesn't exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts
                USING fts5(item_id UNINDEXED, content, metadata)
                """
            )
            conn.execute("DROP TABLE IF EXISTS items_meta")
            conn.commit()

    def index_item(
        self,
        item_id: str,
        content: str,
        metadata: dict[str, Any],
    ) -> None:
        """Insert or update an item in the FTS5 index.

        YAML front-matter (the leading ``--- ... ---`` block) is stripped from
        *content* before it is stored so that FTS snippets never surface raw
        metadata key/value lines as search results.
        """
        indexed_content = _strip_frontmatter(content)
        meta_str = json.dumps(metadata)
        with self._connect() as conn:
            # Delete existing entries for this item_id
            conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            # Insert new entry
            conn.execute(
                "INSERT INTO items_fts (item_id, content, metadata) VALUES (?, ?, ?)",
                (item_id, indexed_content, meta_str),
            )
            conn.commit()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search the FTS5 index and return ranked matches.

        Korean queries are preprocessed (particle stripping + alias expansion)
        before being submitted to FTS5.  When alias expansion produces a
        different query form, both variants are searched and results are merged
        (deduped by item_id) so that ``니모스를`` matches the same documents
        as ``mnemos``.

        Compound technical terms containing colons or hyphens (e.g. ``crew:run``,
        ``agent-crew``) are also normalised by replacing those separators with
        spaces.  This produces an additional query variant so that FTS5 can match
        documents that contain the component tokens even when the full compound
        form is absent.  Both the original (quoted) and normalised forms are
        searched and results are merged.
        """
        if not query.strip():
            return []

        # Expand Korean query into one or two variants (deduped).
        query_variants = expand_query(query)

        # For each variant, also add a compound-normalised form (colon / hyphen
        # replaced with spaces).  This widens recall for queries like "crew:run"
        # or "agent-crew" without discarding the original phrase form.
        expanded_variants: list[str] = []
        seen_variants: set[str] = set()
        for v in query_variants:
            if v not in seen_variants:
                expanded_variants.append(v)
                seen_variants.add(v)
            normalised = _normalise_compound_terms(v)
            # Only add the normalised form when it differs from the original
            # (i.e. the query actually contained colons or hyphens).
            normalised_stripped = " ".join(normalised.split())  # collapse whitespace
            if normalised_stripped and normalised_stripped not in seen_variants:
                expanded_variants.append(normalised_stripped)
                seen_variants.add(normalised_stripped)

        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        with self._connect() as conn:
            for variant in expanded_variants:
                if not variant.strip():
                    continue
                # Sanitise the variant: wrap tokens with FTS5 special chars
                # in double quotes so e.g. "agent-crew" doesn't raise an error.
                safe_variant = _sanitise_fts_variant(variant)
                try:
                    rows = conn.execute(
                        """
                        SELECT item_id, content, metadata, rank
                        FROM items_fts
                        WHERE items_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (safe_variant, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    # Invalid FTS query syntax for this variant — skip it
                    continue

                for row in rows:
                    if row["item_id"] in seen_ids:
                        continue
                    seen_ids.add(row["item_id"])
                    meta = {}
                    try:
                        meta = json.loads(row["metadata"])
                    except (json.JSONDecodeError, TypeError):
                        pass
                    results.append(
                        {
                            "item_id": row["item_id"],
                            "content": row["content"],
                            "metadata": meta,
                            "score": row["rank"],
                            "source": "fts",
                        }
                    )
                if len(results) >= limit:
                    break

        return results[:limit]

    def remove(self, item_id: str) -> None:
        """Remove an item from the FTS5 index."""
        with self._connect() as conn:
            conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            conn.commit()
