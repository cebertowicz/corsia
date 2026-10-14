"""The Update outcome a client has shown, kept on the durable operation row."""

from __future__ import annotations

from automation.self_restart import _now_iso
from infra.database import write_transaction


def mark_outcome_seen(operation_id: str, *, db=None) -> bool | None:
    """Record that a client showed this Update's outcome; the first write wins.

    Returns None for an unknown or unsettled operation, True once the outcome is
    marked seen (whether by this call or an earlier one).
    """
    with write_transaction(db, scope="restart_operation") as connection:
        row = connection.execute(
            "SELECT status FROM self_restart_requests WHERE id=? AND kind='update'",
            (operation_id,),
        ).fetchone()
        if row is None or row["status"] == "in_flight":
            return None
        connection.execute(
            "UPDATE self_restart_requests SET outcome_seen_at=? "
            "WHERE id=? AND outcome_seen_at IS NULL",
            (_now_iso(), operation_id),
        )
        return True
