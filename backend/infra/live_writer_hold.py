"""Hold FIFO inputs a still-live Codex writer refused until that writer releases.

A successor claim can reach Codex while the prior writer still owns the thread
(``CodexLiveTurnBusy`` or the app-server's ``-32600 ... already has an active
writer``). The refusal proves no bytes were written, and the input must stay
queued in order: failing it, or releasing it unheld, would either lose the
input or let its own terminal settlement reclaim it in a loop. So the row keeps
the existing ``proved_no_write`` hold, which stops the FIFO at that row, and the
writer's terminal release lifts it and drives the thread.
"""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction
from infra.vendor_login import without_login_sql

LIVE_WRITER_HOLD = "live_writer"


def hold_for_live_writer(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> tuple[str, ...]:
    """Return the refused rows to the queue, held until the writer's terminal release."""
    for row in rows:
        db.execute(
            f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,"
            "delivery_attempt_id=NULL,write_started_at=NULL,write_outcome='proved_no_write',"
            "delivery_error=?,next_attempt_at=NULL,delivery_revision=delivery_revision+1 "
            "WHERE id=?",
            (LIVE_WRITER_HOLD, row["id"]),
        )
    return ()


def release_live_writer_holds(thread_id: str, *, db: sqlite3.Connection | None = None) -> bool:
    """Lift this thread's live-writer holds; true when any input became claimable."""
    with write_transaction(db, scope="release_live_writer_holds") as connection:
        return connection.execute(
            "UPDATE messages SET write_outcome=NULL,delivery_error=NULL,"
            "delivery_revision=delivery_revision+1 WHERE thread_id=? "
            "AND delivery_state='queued' AND active_turn_id IS NULL "
            "AND write_outcome='proved_no_write' AND delivery_error=?",
            (thread_id, LIVE_WRITER_HOLD),
        ).rowcount > 0
