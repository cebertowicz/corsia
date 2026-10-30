"""Project plan outcomes into human-visible thread update cards."""

from __future__ import annotations

import json
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


def _deferred_person_step(connection, node_id: str) -> bool:
    """Whether the node is a workflow person step built with its card held for its dependencies."""
    row = connection.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='node_created' "
        "ORDER BY created_at,id LIMIT 1", (node_id,),
    ).fetchone()
    created = json.loads(row["payload_json"] or "{}") if row is not None else {}
    return created.get("workflow_by") == "person" and created.get("person_step") is False


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
    # A workflow's person step that waits on earlier steps is asked when they finish.
    if (
        event == "node_created" and (payload is None or payload.get("person_step", True))
    ) or (event == "blocked_cleared" and _deferred_person_step(connection, node_id)):
        row = connection.execute(
            "SELECT kind, assignee, state FROM plan_nodes WHERE id=?", (node_id,),
        ).fetchone()
        if row is not None and row["kind"] == "task" and row["assignee"] == "user" and row["state"] == "pending":
            kind = "person_step"
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
