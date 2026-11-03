"""The one Messages row of this Mac: find the one a thread may use, or add it."""

from __future__ import annotations

import sqlite3

from connections.connected_accounts import MESSAGES_PROVIDER, create_account_for_thread, visible_accounts
from infra.database import get_db
from infra.db_async import run_in_db

ADDRESS = "This Mac"


def _own_db(job, thread_id: str):
    db = get_db()
    try:
        return job(thread_id, db)
    finally:
        db.close()


def _usable(thread_id: str, db: sqlite3.Connection) -> dict | None:
    return next(
        (row for row in visible_accounts(thread_id, db=db, provider=MESSAGES_PROVIDER) if row["state"] == "connected"),
        None,
    )


async def usable_for_thread(thread_id: object) -> dict | None:
    """The connected Messages row whose reach includes this thread, or None."""
    return await run_in_db(_own_db, _usable, thread_id) if isinstance(thread_id, str) and thread_id else None


def _connect(thread_id: str, db: sqlite3.Connection) -> tuple[dict | None, bool]:
    """``(usable row, created)``: add the row only when this Mac has none, at the adding thread's reach."""
    if (found := _usable(thread_id, db)) is not None:
        return found, False
    if visible_accounts(None, db=db, provider=MESSAGES_PROVIDER):
        return None, False
    row = create_account_for_thread(
        MESSAGES_PROVIDER, ADDRESS, actor_thread_id=thread_id, created_by=("agent", thread_id), db=db,
    )
    return row, True


async def connect(thread_id: str) -> tuple[dict | None, bool]:
    return await run_in_db(_own_db, _connect, thread_id)
