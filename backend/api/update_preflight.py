"""Checks that must finish before an Update begins its durable drain."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from infra.update_status import UpdateStatus

logger = logging.getLogger(__name__)


async def fresh_update_target() -> UpdateStatus:
    """Probe the feed fresh and return the newer build an Update will install.

    The installer reads the feed fresh when it stages, so a cached probe can
    name a build that is no longer the newest. With no build to name,
    verification could only fail and roll a good build back, so refuse.
    """
    from infra.update_status import update_status

    fresh = await asyncio.to_thread(update_status, True)
    if fresh.available is not True:
        logger.warning(
            "update refused before acquire: available=%s error=%s", fresh.available, fresh.error
        )
        if fresh.error:
            raise HTTPException(503, "update_check_failed")
        raise HTTPException(409, "update_not_available")
    return fresh


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
