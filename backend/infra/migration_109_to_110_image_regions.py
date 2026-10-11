"""Schema 109 -> 110: image-region anchors on document comments (DCOL-FR13.3/13.4)."""

from __future__ import annotations

import sqlite3

# Added in this order, after every earlier column, so a migrated table matches a fresh one.
REGION_COLUMNS: tuple[str, ...] = (
    "region_x", "region_y", "region_w", "region_h", "region_number", "image_rev", "image_src",
)
_TYPES = {"region_number": "INTEGER", "image_rev": "TEXT", "image_src": "TEXT"}

REGION_NUMBER_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_doc_comments_region_number "
    "ON doc_comments(artifact_id, COALESCE(image_src, ''), region_number) "
    "WHERE region_number IS NOT NULL"
)


def migrate_109_to_110(conn: sqlite3.Connection) -> None:
    """Add the nullable region columns and the one-number-per-image index."""
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_comments'"
    ).fetchone()
    if table is None:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(doc_comments)")}
    for column in REGION_COLUMNS:
        if column not in columns:
            conn.execute(f"ALTER TABLE doc_comments ADD COLUMN {column} {_TYPES.get(column, 'REAL')}")
    conn.execute(REGION_NUMBER_INDEX_SQL)
