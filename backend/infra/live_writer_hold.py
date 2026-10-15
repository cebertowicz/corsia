"""Hold FIFO inputs a still-live Codex writer refused until that writer releases.

A successor claim can reach Codex while the prior writer still owns the thread
(``CodexLiveTurnBusy`` or the app-server's ``-32600 ... already has an active
writer``). The refusal proves no bytes were written, and the input must stay
queued in order: failing it, or releasing it unheld, would either lose the
input or let its own terminal settlement reclaim it in a loop. So the row keeps
the existing ``proved_no_write`` hold, which stops the FIFO at that row, and the
writer's terminal release lifts it and drives the thread.

A hold is only safe while this process owns that writer: its terminal release is
the hold's single release path. ``hold_for_live_writer`` therefore checks the
live registry inside the same write transaction that writes the hold. Release
runs after ``finalize_live`` in a later serialized write, so either the hold
commits first and the release lifts it, or the writer is already gone and the
caller takes the ordinary fail-visible path. A writer this process does not own
(a ``-32600`` from another backend) never releases anything, and boot recovery
clears every hold left by a dead epoch.

``release_checkpoint_holds`` matches these rows too, so a checkpoint install can
lift a hold early; that is harmless, the input is refused and held once more.
"""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction
from infra.vendor_login import without_login_sql

LIVE_WRITER_HOLD = "live_writer"


def _owns_prior_writer(thread_id: str, successor_turn_ids: set[str]) -> bool:
    """Whether this process holds a live writer, other than the refused input's own Turn."""
    from subagents.codex_runtime_state import _LIVE

    live = _LIVE.get(thread_id)
    turn = live.corsia_turn if live is not None else None
    return turn is not None and turn.turn_id not in successor_turn_ids


def hold_for_live_writer(
    db: sqlite3.Connection, rows: list[sqlite3.Row],
) -> tuple[str, ...] | None:
    """Return the refused rows to the queue, held until the writer's terminal release.

    Returns ``None`` without writing when this process owns no prior writer to
    release the hold; the caller then fails the input visibly.
    """
    if not db.in_transaction:
        raise RuntimeError("live-writer hold requires the input writer's transaction")
    thread_id = str(rows[0]["thread_id"])
    successors = {str(row["active_turn_id"]) for row in rows if row["active_turn_id"]}
    if not _owns_prior_writer(thread_id, successors):
        return None
    for row in rows:
        db.execute(
            f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,"
            "delivery_attempt_id=NULL,write_started_at=NULL,write_outcome='proved_no_write',"
            "delivery_error=?,next_attempt_at=NULL,delivery_revision=delivery_revision+1 "
            "WHERE id=?",
            (LIVE_WRITER_HOLD, row["id"]),
        )
    return ()


def clear_live_writer_holds(db: sqlite3.Connection, thread_id: str | None = None) -> bool:
    """Lift live-writer holds (one thread's, or all); true when any input became claimable."""
    if not db.in_transaction:
        raise RuntimeError("live-writer hold release requires a write transaction")
    return db.execute(
        "UPDATE messages SET write_outcome=NULL,delivery_error=NULL,"
        "delivery_revision=delivery_revision+1 WHERE "
        + ("" if thread_id is None else "thread_id=? AND ")
        + "delivery_state='queued' AND active_turn_id IS NULL "
        "AND write_outcome='proved_no_write' AND delivery_error=?",
        (() if thread_id is None else (thread_id,)) + (LIVE_WRITER_HOLD,),
    ).rowcount > 0


def release_live_writer_holds(thread_id: str, *, db: sqlite3.Connection | None = None) -> bool:
    """Lift this thread's live-writer holds; true when any input became claimable."""
    with write_transaction(db, scope="release_live_writer_holds") as connection:
        return clear_live_writer_holds(connection, thread_id)
