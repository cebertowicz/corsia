"""Fill ``comment_fts`` for chat comments stored before the index existed.

The 203 -> 204 step creates only the index and its triggers, so it adds nothing to the startup
write lock. This pass runs after startup, off the migration ladder: it walks ``chat_comments``
by rowid up to the highest rowid it saw when it began (its ceiling), ``BATCH_ROWS`` rows per
write transaction, and records where it got to in the same transaction. A restart continues
from there rather than starting over. Rows above the ceiling were indexed by the insert
trigger; a rowid the trigger already indexed is written again with the same words, never twice.

Until it finishes, a history search returns message hits plus whatever comments are filled.
At the end it logs once: rows filled, and how many comments in input metadata have no
``chat_comments`` row (their target did not resolve, so they are not indexed).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

from loguru import logger as log

from infra.database import write_transaction
from infra.db_fts import comment_search_words_sql
from infra.settings import get_setting, set_setting

#: Progress, then completion: ``{"ceiling", "through", "filled"}``, plus ``completed_at``.
MARKER = "comment_search_fill"
#: Rows per write transaction. Sized so one batch holds the write lock well under 100 ms on a
#: 1M-message database (measured figure in the PR that added this).
BATCH_ROWS = 1500
#: Pause between batches, so other writers get the lock between them.
PAUSE_SECONDS = 0.05

_FILL_SQL = (
    "INSERT OR REPLACE INTO comment_fts(rowid, words) "
    "SELECT rowid, w FROM (SELECT cc.rowid AS rowid, "
    f"{comment_search_words_sql('cc.user_message_id', 'cc.cid')} AS w "
    "FROM chat_comments cc WHERE cc.rowid > ? AND cc.rowid <= ?) WHERE w IS NOT NULL"
)

_UNPLACED_SQL = (
    "SELECT count(*) FROM messages m, json_each(CASE WHEN json_valid(m.metadata) THEN m.metadata "
    "ELSE '{}' END, '$.reply_context.comments') j "
    "WHERE m.role = 'user' AND m.metadata LIKE '%\"comments\"%' "
    "AND NOT EXISTS (SELECT 1 FROM chat_comments cc WHERE cc.user_message_id = m.id "
    "AND cc.cid = CASE WHEN j.type = 'object' THEN json_extract(j.value, '$.cid') END)"
)


#: What ``fill_batch`` found: a batch written, nothing left to fill, or an earlier run finished.
MORE, FILLED, COMPLETE = "more", "filled", "complete"


def fill_batch(db: sqlite3.Connection | None = None) -> str:
    """Fill the next batch in its own write transaction and say what is left."""
    with write_transaction(db, scope="comment_search_fill") as conn:
        state = get_setting(MARKER, db=conn) or {}
        if state.get("completed_at"):
            return COMPLETE
        ceiling = state.get("ceiling")
        if not isinstance(ceiling, int):
            ceiling = int(conn.execute("SELECT coalesce(max(rowid), 0) FROM chat_comments").fetchone()[0])
        through = state.get("through") if isinstance(state.get("through"), int) else 0
        upper = conn.execute(
            "SELECT max(rowid) FROM (SELECT rowid FROM chat_comments "
            "WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?)",
            (through, ceiling, BATCH_ROWS),
        ).fetchone()[0]
        if upper is None:
            return FILLED
        filled = int(state.get("filled") or 0) + conn.execute(_FILL_SQL, (through, upper)).rowcount
        set_setting(MARKER, {"ceiling": ceiling, "through": int(upper), "filled": filled}, db=conn)
    return MORE


def count_unplaced(db: sqlite3.Connection) -> int:
    """Comments in person inputs' metadata that have no ``chat_comments`` row."""
    return int(db.execute(_UNPLACED_SQL).fetchone()[0])


def finish(unplaced: int, db: sqlite3.Connection | None = None) -> int | None:
    """Mark the fill complete; return rows filled, or None when an earlier run already finished."""
    with write_transaction(db, scope="comment_search_fill_done") as conn:
        state = get_setting(MARKER, db=conn) or {}
        if state.get("completed_at"):
            return None
        filled = int(state.get("filled") or 0)
        set_setting(MARKER, {
            **state, "filled": filled, "unplaced": unplaced,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, db=conn)
    return filled


def _read_count_unplaced() -> int:
    from infra.database import get_db

    return count_unplaced(get_db())


async def run_fill() -> None:
    """Fill every batch, then log the totals once. A failure is retried on the next start."""
    from infra.db_async import run_bulk, run_in_db

    try:
        while (found := await run_in_db(fill_batch)) == MORE:
            await asyncio.sleep(PAUSE_SECONDS)
        if found == COMPLETE:
            return
        unplaced = await run_bulk(_read_count_unplaced)
        filled = await run_in_db(finish, unplaced)
        if filled is not None:
            log.info("comment search fill done: rows filled={} comments without a chat_comments row={}",
                     filled, unplaced)
    except Exception:
        log.exception("comment search fill failed; it resumes on the next start")
