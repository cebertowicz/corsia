"""Add the nullable author-declared size to structured-plan nodes."""
from __future__ import annotations


def migrate_90_to_91(conn) -> None:
    """Keep historical nodes unsized while admitting the canonical classes."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "plan_nodes" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(plan_nodes)")}
    if "size" not in columns:
        conn.execute(
            "ALTER TABLE plan_nodes ADD COLUMN size TEXT CHECK ("
            "size IS NULL OR size IN ('XS', 'S', 'M', 'L', 'XL', 'XXL', 'unknown')"
            ")"
        )
