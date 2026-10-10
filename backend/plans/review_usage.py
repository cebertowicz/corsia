"""Attribute an exact review Turn's usage to the commitment it verified."""

from __future__ import annotations

import plans.service as service
from infra.turn_plan_binding import bind_turn_to_plan_attempt


def bind(
    node_id: str,
    *,
    reviewer_thread_id: str | None,
    turn_id: str | None,
    db,
) -> None:
    """Attribute an unbound live review Turn to the verified node's commitment.

    Accounting only, so it never refuses the acceptance it accompanies. A
    reviewer Turn that is missing, no longer live, or already bound to an
    attempt is left unattributed. An unbound live Turn is bound with the
    `verification` role; the binding owner attributes its earlier usage.
    """

    if reviewer_thread_id is None or turn_id is None:
        return
    node = service._node_row(db, node_id)
    turn = db.execute(
        """SELECT 1 FROM active_turns
           WHERE id=? AND thread_id=?
             AND status='running'""",
        (turn_id, reviewer_thread_id),
    ).fetchone()
    if turn is None:
        return
    attempt = db.execute(
        """SELECT * FROM plan_node_attempts
           WHERE node_id=? ORDER BY attempt_ordinal DESC LIMIT 1""",
        (node_id,),
    ).fetchone()
    if attempt is None:
        return
    bind_turn_to_plan_attempt(
        db,
        turn_id=turn_id,
        plan_id=node["plan_id"],
        node_id=node_id,
        attempt_id=attempt["id"],
        commitment_id=node["commitment_id"],
        usage_role="verification",
    )
