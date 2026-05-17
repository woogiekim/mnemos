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
        """Insert or update an item in the FTS5 index."""
        meta_str = json.dumps(metadata)
        with self._connect() as conn:
            # Delete existing entries for this item_id
            conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            # Insert new entry
            conn.execute(
                "INSERT INTO items_fts (item_id, content, metadata) VALUES (?, ?, ?)",
                (item_id, content, meta_str),
            )
            conn.commit()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search the FTS5 index and return ranked matches.

        Korean queries are preprocessed (particle stripping + alias expansion)
        before being submitted to FTS5.  When alias expansion produces a
        different query form, both variants are searched and results are merged
        (deduped by item_id) so that ``니모스를`` matches the same documents
        as ``mnemos``.
        """
        if not query.strip():
            return []

        # Expand Korean query into one or two variants (deduped).
        query_variants = expand_query(query)

        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        with self._connect() as conn:
            for variant in query_variants:
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
