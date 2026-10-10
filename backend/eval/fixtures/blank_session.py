
from __future__ import annotations

import contextlib
from infra.time import utc_now_iso
from typing import AsyncIterator


@contextlib.asynccontextmanager
async def blank_session(
    *,
    user_id: str,
    thread_id: str,
    title: str | None = None,
    spawned_by: str | None = None,
) -> AsyncIterator[dict]:
    from infra.database import get_db
    from infra.db_async import run_in_db

    title = title or "eval blank session"
    now = utc_now_iso()

    def _setup():
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO users (id, email, created_at) VALUES (?, ?, ?)",
            (user_id, f"{user_id}@eval.local", now),
        )
        db.execute(
            "INSERT INTO threads (id, user_id, title, spawned_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, user_id, title, spawned_by, now, now),
        )
        db.commit()
        db.close()

    await run_in_db(_setup)

    try:
        yield {
            "session_id": thread_id,
            "user_id": user_id,
            "spawned_by": spawned_by,
        }
    finally:
        def _teardown():
            db = get_db()
            db.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
            db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            db.commit()
            db.close()

        await run_in_db(_teardown)
