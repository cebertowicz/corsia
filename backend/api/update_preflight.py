"""Checks that must finish before an Update begins its durable drain."""

from __future__ import annotations

import asyncio
from pathlib import Path


async def check_update_backup_room() -> None:
    """Refuse an update before drain when its startup migration cannot back up."""
    from infra.database import get_db
    from infra.database_migrations import require_backup_room

    def check() -> None:
        connection = get_db()
        try:
            row = connection.execute("PRAGMA database_list").fetchone()
            source = Path(row[2]) if row and row[2] else None
            if source is None:
                return
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            require_backup_room(source, version)
        finally:
            connection.close()

    await asyncio.to_thread(check)
