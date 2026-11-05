"""Settle FIFO inputs a checkpoint refusal left unwritten.

A checkpoint refusal reaches settlement as ``CheckpointUnavailable``. Holding
every refusal was wrong: by the time the error is raised the outcome is already
decided for this history, window and model, and nothing in the system will
revisit it on its own. A held input then waits for an event that never arrives,
and because the FIFO stops at the first held row, the whole thread goes silent.

So the refusal is classified instead:

- an install or cap change that landed during the attempt **releases** the rows,
  since the fold that failed is no longer the fold that would run;
- a **preparing** model switch keeps them held, because its terminal settlement
  transfers the unwritten suffix to the destination -- a real owner with a
  reachable release;
- anything else **fails those exact inputs visibly**, using the delivery state
  and actions the UI already renders, and the rest of the FIFO proceeds.
"""

from __future__ import annotations

import sqlite3

from core.checkpoint_types import CheckpointUnavailable
from infra.vendor_login import without_login_sql


def _recovery_changed(
    db: sqlite3.Connection, rows: list[sqlite3.Row], error: CheckpointUnavailable,
) -> bool:
    """Whether an actual install or cap change outran this refusal.

    Both sides are the configured window: a live memory fit ends with the Turn
    it served, and its absence here is no change to recover on.
    """
    from infra.checkpoint_queries import current_installed
    from llm.context_window import resolve_window_for_row
    from llm.resolution import _target_row

    head = current_installed(str(rows[0]["thread_id"]), db=db)
    model = _target_row(str(rows[0]["target_model"]), db=db)
    window = resolve_window_for_row(model, db=db) if model is not None else None
    return (
        (head.id if head else None) != error.installed_checkpoint_id
        or (window is not None and window != error.context_window)
    )


def _switch_is_preparing(db: sqlite3.Connection, thread_id: str) -> bool:
    """Whether a pending switch still owes these inputs a transfer."""
    row = db.execute(
        "SELECT switch_status FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    return row is not None and row["switch_status"] == "preparing"


def _admission_holds(
    db: sqlite3.Connection, rows: list[sqlite3.Row], error: CheckpointUnavailable,
) -> bool:
    """Whether admission will hold these rows on the limit that stopped the fold.

    Same target: admission reads the verdict the gate read. Differing target:
    only when the rows' own model is limited on that connection. Anything else
    would release into a refusal loop, so it stays on the fail-visible path.
    """
    from infra.held_admission import limited_admission

    model = str(rows[0]["target_model"])
    if model == error.limited_target:
        return True
    held = limited_admission(str(rows[0]["thread_id"]), model, db=db)
    return held is not None and held.connection == error.limited_connection


def _hold(
    db: sqlite3.Connection, rows: list[sqlite3.Row], error: CheckpointUnavailable,
) -> None:
    """Conserve the rows as proved unwritten until their transfer settles."""
    for row in rows:
        db.execute(
            f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,write_started_at=NULL,"
            "write_outcome='proved_no_write',delivery_error=?,next_attempt_at=NULL,"
            "delivery_revision=delivery_revision+1 WHERE id=?",
            (str(error), row["id"]),
        )


def _release(db: sqlite3.Connection, rows: list[sqlite3.Row]) -> None:
    """Return the rows to ordinary queued delivery; recovery already won."""
    for row in rows:
        db.execute(
            f"UPDATE messages SET metadata={without_login_sql()},active_turn_id=NULL,write_started_at=NULL,"
            "write_outcome=NULL,delivery_error=NULL,next_attempt_at=NULL,"
            "delivery_revision=delivery_revision+1,delivery_attempt_id=NULL "
            "WHERE id=?",
            (row["id"],),
        )


def settle_checkpoint_refusal(
    db: sqlite3.Connection, rows: list[sqlite3.Row], error: CheckpointUnavailable,
) -> tuple[str, ...]:
    """Release, hold, or visibly fail inputs one checkpoint refusal did not write.

    Returns the ids it failed, for the caller to drive the report owner after commit.
    """
    if _recovery_changed(db, rows, error):
        _release(db, rows)
        return ()
    if _switch_is_preparing(db, str(rows[0]["thread_id"])):
        _hold(db, rows, error)
        return ()
    if error.limited_target is not None and _admission_holds(db, rows, error):
        # A limited connection has a deadline and probe that revisit the input:
        # release it, and admission holds it (the slice 2a surface).
        _release(db, rows)
        return ()
    from infra.input_sequencing import sequence_attempt

    # Deterministic for this history, window and model: no install, cap change
    # or transfer is coming, so waiting would be waiting forever.
    return sequence_attempt(
        db, rows, state="failed", outcome="proved_no_write", error=str(error),
    )
