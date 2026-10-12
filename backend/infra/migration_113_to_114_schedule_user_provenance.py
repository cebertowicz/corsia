"""Record user-origin provenance for existing automation schedules."""

from __future__ import annotations


def migrate_113_to_114(conn) -> None:
    """Add the fail-closed provenance flag; legacy schedules remain held."""
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(automation_schedules)")
    }
    if columns and "asked_by_user" not in columns:
        conn.execute(
            "ALTER TABLE automation_schedules "
            "ADD COLUMN asked_by_user INTEGER NOT NULL DEFAULT 0"
        )
