"""Project plan outcomes into human-visible thread update cards."""

from __future__ import annotations

from collections.abc import Mapping

from infra.thread_updates import PlanUpdateKind, record_plan_update


# Child threads report through their parent's wake and must not create user cards.
VISIBLE_THREAD_OWNER = "t.spawned_by IS NULL"

_STATE_KINDS: dict[str, PlanUpdateKind] = {
    "accepted": "done",
    "accepted_with_followup": "done_follow_up",
    "failed": "failed",
    "closed": "closed",
}


def _outcome_kind(event: str, new_state: str | None, payload: Mapping | None) -> PlanUpdateKind | None:
    if event == "state_transition":
        return _STATE_KINDS.get(str(new_state or ""))
    if event == "hold_placed" and payload and payload.get("kind") == "blocked":
        return "blocked"
    return None


def _eligible_thread_id(connection, node_id: str, *, any_depth: bool) -> str | None:
    # A nested item stays quiet unless it is the user's: assigned to them, or blocked
    # (every blocked hold is a user ask; agent-owned blockers are waits).
    row = connection.execute(
        f"""SELECT p.owner_id AS thread_id
              FROM plan_nodes n
              JOIN plans p ON p.id=n.plan_id
              JOIN threads t ON t.id=p.owner_id
             WHERE n.id=?
               AND p.owner_kind='thread'
               AND t.archived_at IS NULL
               AND {VISIBLE_THREAD_OWNER}
               AND (n.parent_id IS NULL OR n.assignee='user' OR ?)""",
        (node_id, int(any_depth)),
    ).fetchone()
    return str(row["thread_id"]) if row is not None else None


def _duplicate_open_block(connection, node_id: str, payload: Mapping) -> bool:
    hold_id = payload.get("hold_id")
    reason_key = payload.get("reason_key")
    if not isinstance(hold_id, str) or not isinstance(reason_key, str):
        return False
    row = connection.execute(
        """SELECT id FROM plan_node_holds
             WHERE node_id=? AND kind='blocked' AND reason_key=?
               AND cleared_at IS NULL AND id != ?""",
        (node_id, reason_key, hold_id),
    ).fetchone()
    return row is not None


def observe_node_event(
    connection,
    *,
    node_id: str,
    event: str,
    event_id: str,
    new_state: str | None,
    payload: Mapping | None,
    created_at: str,
) -> None:
    """Write the one card a qualifying outcome permits, in the caller's transaction."""
    kind = _outcome_kind(event, new_state, payload)
    if kind is None:
        return
    thread_id = _eligible_thread_id(connection, node_id, any_depth=kind == "blocked")
    if thread_id is None:
        return
    if kind == "blocked" and payload is not None and _duplicate_open_block(connection, node_id, payload):
        return
    record_plan_update(
        connection,
        thread_id=thread_id,
        kind=kind,
        event_id=event_id,
        node_id=node_id,
        created_at=created_at,
    )
