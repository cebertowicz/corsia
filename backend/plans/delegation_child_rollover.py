"""Read the claimed successor that makes a delegated-worker refusal notice false."""

from __future__ import annotations


def has_rollover_successor(connection, thread_id: str, turn_id: str | None) -> bool:
    """Whether the errored Turn has a later, differently-accounted successor.

    Terminal admission claims a rollover successor before the plan terminal hook
    emits its parent notice. A later non-error Turn on another account is thus
    the durable proof that the worker recovered without parent intervention.
    """
    if turn_id is None:
        return False
    turn = connection.execute(
        """SELECT turn_order,vendor_account_id FROM active_turns
           WHERE id=? AND thread_id=?""",
        (turn_id, thread_id),
    ).fetchone()
    if turn is None or turn["vendor_account_id"] is None:
        return False
    successor = connection.execute(
        """SELECT 1 FROM active_turns
           WHERE thread_id=? AND turn_order>? AND status<>'error'
             AND vendor_account_id IS NOT NULL AND vendor_account_id<>?
           LIMIT 1""",
        (thread_id, turn["turn_order"], turn["vendor_account_id"]),
    ).fetchone()
    return successor is not None
