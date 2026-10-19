"""Which plan nodes a lane settles, and what the generic boundary may not do.

One lane creates a plan node whose *settlement is not ordinary plan work*: a
structured decision's user review node (``plans.decisions``). It is genuinely a
plan task — PRD-PLANS A10 says a gate assigned to the user *is* a plan task, and
it is read, ordered, counted, indicated, and progressed like any other node.

What is not ordinary is who ends it. The node's terminal state is the record of
an answer the lane holds: a decision node becomes ``accepted`` only when the
user's chosen option is recorded. The lane transitions the node itself, inside
the same transaction that writes the answer.

So a generic transition on such a node is not a competing opinion about plan
state — it removes the only state the lane's own writer can legally move from,
which then makes the lane's write raise on a durable invariant it cannot repair.
The rule is therefore a *boundary* rule, not a lane rule: the generic plan
boundary may read, position, and extend these nodes, and may never drive or end
them while the question is open.

Structure-only operations stay available, because they make no claim about the
question: ``reorder`` moves the node among siblings and ``split`` adds one.
Everything that changes the node's own state, or restates its own intent, is
refused — and the refusal names the operation that does own it. ``archive_node``
ends the node outright, and ``add_follow_up`` declares residual work from an
answer that has not been given, so both are refused too. ``close_node`` is the
same act as the archive by a different name -- it ends the node outright -- so
it is refused for the same reason.

The lane still refuses an agent's pause and resume, but admits the user's: a
pause is a hold that defers the question's attention until resume, the node
keeps the state the answer settles from, and the answer still lands while it
is paused.

This module only classifies and refuses. It writes nothing, so it adds no second
owner of decision state.
"""

from __future__ import annotations

from typing import NoReturn

import plans.service as service

#: Operations that drive, end, or restate a node's own intent. Mirrors the
#: dispatch table in ``plans.boundary``; structure-only operations are absent by
#: design, not by omission.
SETTLING_OPERATIONS = frozenset({
    "edit", "activate", "start", "pause_node", "resume_node", "block_node",
    "resolve_blocker", "request_review", "reject", "fail", "accept",
    "accept_with_follow_up", "add_follow_up", "add_correction", "close_node",
    "archive_node", "attach_spawned_thread", "replace_spawned_thread",
    "arm_watch", "resolve_watch",
    "mark_done",
})

DECISION = "decision"

#: The user's own steering a decision node admits: Won't do withdraws the
#: decision (``plans.user_steering``); pause and resume only hold it.
_USER_STEERING = frozenset({"close_node", "pause_node", "resume_node"})

_LANE_SQL = """
    SELECT i.type AS artifact_type, i.metadata AS metadata
      FROM plan_node_artifact_links l
      JOIN artifact_identities i ON i.id = l.artifact_id AND i.state = 'active'
     WHERE l.node_id = ? AND l.role = 'attention'
"""

#: Names the operation that does own the outcome, so the refusal is actionable.
#: The message carries no node, plan, or decision identity.
_REFUSALS = {
    DECISION: (
        "this plan node records a decision awaiting the user; use the decision "
        "operations (update, reorder, invalidate) instead of {operation}"
    ),
}


class LaneOwnedNodeError(service.PlanInputError):
    """A generic mutation was refused because a lane owns this node's outcome.

    A fixable refusal, not a conflict: nothing moved, and the caller can reach
    the same outcome through the operation the message names. It subclasses the
    input class rather than sitting beside it, so every surface that already
    understands "fix the request" understands this too; a sibling of the input
    class has to be re-listed at each caller, and the surface that forgets tells
    an agent to re-read and retry a refusal re-reading cannot fix.
    """


def owning_lane(db, node_id: str) -> str | None:
    """Return the lane still owning this node's settlement, or None.

    Only an *open* question owns a node. Once the decision is answered or
    withdrawn, the node is an ordinary terminal plan node and this returns None.
    """
    from plans import decision_projection

    for row in db.execute(_LANE_SQL, (node_id,)).fetchall():
        if row["artifact_type"] == decision_projection.DECISION_TYPE:
            body = decision_projection._body(row)
            if body is not None and body["state"] in decision_projection.OPEN_STATES:
                return DECISION
    return None


def lane_refusing(
    db, node_id: str, operation: str, *, by_user: bool = False,
) -> str | None:
    """Return the lane that refuses this generic operation on the node, or None.

    Non-raising, for ``plans.boundary_guard``; ``refuse`` raises the refusal.
    The question was put to the user, so the user may steer it: Won't do
    (``plans.user_steering`` withdraws the decision rather than closing its
    node under it) and pause/resume, which hold the node without moving it.
    """
    if operation not in SETTLING_OPERATIONS:
        return None
    lane = owning_lane(db, node_id)
    if lane == DECISION and by_user and operation in _USER_STEERING:
        return None
    return lane


#: What an agent's ``mark_done`` on a decision node also learns: the user's
#: answer settles it, and approving something the agent produced is an artifact
#: ask, not a decision (System 40-HUMAN-AGENT-INTERFACE). The open decision
#: holds its task, so the ask can follow only once the decision is invalidated.
_APPROVAL_NOTE = (
    "; the user's answer settles it. To have the user review or approve "
    "something you produced instead, invalidate this decision, then block "
    "the task on them with ask_artifact_id"
)


def refusal_message(lane: str, operation: str) -> str:
    """Return the refusal text, for a caller that reports rather than raises."""
    note = _APPROVAL_NOTE if operation == "mark_done" else ""
    return _REFUSALS[lane].format(operation=operation) + note


def refuse(lane: str, operation: str) -> NoReturn:
    """Refuse a generic operation that would drive or end a lane-owned node."""
    raise LaneOwnedNodeError(refusal_message(lane, operation))
