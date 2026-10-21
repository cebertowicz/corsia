"""Checks that must finish before an Update begins its durable drain."""

from __future__ import annotations

from pathlib import Path


def _check_update_backup_room() -> None:
    """Refuse an update when its startup migration backup cannot fit."""
    from infra.database import get_db
    from infra.database_migrations import require_backup_room

    connection = get_db()
    row = connection.execute("PRAGMA database_list").fetchone()
    source = Path(row[2]) if row and row[2] else None
    if source is None:
        return
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    require_backup_room(source, version)


async def check_update_backup_room() -> None:
    """Run the durable pre-drain room check on CORSIA's SQLite work lane."""
    from infra.db_async import run_in_db

    await run_in_db(_check_update_backup_room)
