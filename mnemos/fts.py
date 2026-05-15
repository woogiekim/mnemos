"""SQLite FTS5 index for full-text search of memory items."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


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
        """Create FTS5 table and metadata table if they don't exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts
                USING fts5(item_id UNINDEXED, content, metadata)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS items_meta (
                    item_id TEXT PRIMARY KEY,
                    metadata TEXT
                )
                """
            )
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
            conn.execute("DELETE FROM items_meta WHERE item_id = ?", (item_id,))
            # Insert new entry
            conn.execute(
                "INSERT INTO items_fts (item_id, content, metadata) VALUES (?, ?, ?)",
                (item_id, content, meta_str),
            )
            conn.execute(
                "INSERT INTO items_meta (item_id, metadata) VALUES (?, ?)",
                (item_id, meta_str),
            )
            conn.commit()

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Search the FTS5 index and return ranked matches."""
        if not query.strip():
            return []
        with self._connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT item_id, content, metadata, rank
                    FROM items_fts
                    WHERE items_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Invalid FTS query syntax — return empty
                return []

        results = []
        for row in rows:
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
        return results

    def remove(self, item_id: str) -> None:
        """Remove an item from the FTS5 index."""
        with self._connect() as conn:
            conn.execute("DELETE FROM items_fts WHERE item_id = ?", (item_id,))
            conn.execute("DELETE FROM items_meta WHERE item_id = ?", (item_id,))
            conn.commit()
