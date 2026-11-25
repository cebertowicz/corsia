"""Where a comment task lands in its plan: under the task the document belongs to.

Plan order is priority, and the newest thing is not first: a task opened by
comments on a document takes its place right after the task that owns that
document in the recipient's plan, not at the top and not wherever the plan
ends. At that spot the person's own comment tasks come before ones opened by
another agent's comments, each kind oldest first.

The owning task is the owner registration's origin node (the task the document
was registered for), else the newest task in the plan that took the document as
a result. A task inside a workflow run lands after the run, so the run's steps
keep their order. With no owning task in the plan, the task goes last.
Nothing here writes.
"""

from __future__ import annotations

import plans.aggregates as aggregates
import plans.node_states as node_states
from plans.review_request_framing import REVIEW_PROVENANCE
from registry.identity_bridge import OWNER_REGISTRATION_STATES

#: Comment-task kinds at a spot, in the order they sit after the owning task.
PERSON, AGENT, OTHER = 0, 1, 2


def owning_node(db, plan_id: str, artifact_id: str):
    """Return the node in ``plan_id``'s work the document belongs to, or None."""
    marks = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    in_work = f"{node_states.in_work_sql('n')} AND {node_states.outside_retired_branch_sql('n')}"
    row = db.execute(
        f"""SELECT n.* FROM artifact_registrations r JOIN plan_nodes n ON n.id=r.origin_plan_node_id
             WHERE r.artifact_id=? AND n.plan_id=? AND r.retired_at IS NULL
               AND r.relationship_state IN ({marks}) AND {in_work}
             ORDER BY CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END, r.updated_at DESC
             LIMIT 1""",
        (artifact_id, plan_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()
    if row is not None:
        return row
    return db.execute(
        f"""SELECT n.* FROM plan_node_artifact_links l JOIN plan_nodes n ON n.id=l.node_id
            WHERE l.artifact_id=? AND l.role='result' AND n.plan_id=? AND {in_work}
            ORDER BY l.created_at DESC, n.id DESC LIMIT 1""",
        (artifact_id, plan_id),
    ).fetchone()


def _outside_runs(db, node):
    """Climb out of workflow runs: a run's steps are its own sequence."""
    while node["parent_id"] is not None:
        parent = db.execute("SELECT * FROM plan_nodes WHERE id=?", (node["parent_id"],)).fetchone()
        if parent is None or parent["workflow_ref"] is None:
            break
        node = parent
    return node


def _kind(db, node_id: str) -> int:
    """Whether a sibling is a person's comment task, another agent's, or other work."""
    batch = db.execute(
        "SELECT author_kind FROM doc_feedback_batches WHERE delivered_node_id=? ORDER BY created_at LIMIT 1",
        (node_id,),
    ).fetchone()
    if batch is not None:
        return PERSON if batch["author_kind"] == "user" else AGENT
    review = db.execute(
        "SELECT 1 FROM plan_node_artifact_links WHERE node_id=? AND provenance=? LIMIT 1",
        (node_id, REVIEW_PROVENANCE),
    ).fetchone()
    return AGENT if review is not None else OTHER


def spot(db, plan_id: str, artifact_id: str, *, person: bool) -> tuple[bool, str | None, int | None]:
    """Return ``(placed, parent_id, position)`` for a new comment task.

    ``placed`` False means no owning task: append as before. Otherwise create it
    under ``parent_id`` at ``position`` among that parent's open children (the
    position ``plans.node_creation`` takes), or last there when it is None.
    """
    owner = owning_node(db, plan_id, artifact_id)
    if owner is None:
        return False, None, None
    owner = _outside_runs(db, owner)
    siblings = db.execute(
        "SELECT id, order_key FROM plan_nodes WHERE plan_id=? AND parent_id IS ? "
        f"AND {aggregates.open_siblings_sql(db)} ORDER BY order_key, id",
        (plan_id, owner["parent_id"]),
    ).fetchall()
    mark = (owner["order_key"], str(owner["id"]))
    # A person's comment task goes after the person's ones already there; another
    # agent's goes after every comment task already there.
    last = PERSON if person else AGENT
    for position, sibling in enumerate(siblings):
        if (sibling["order_key"], str(sibling["id"])) <= mark:
            continue
        if _kind(db, str(sibling["id"])) > last:
            return True, owner["parent_id"], position
    return True, owner["parent_id"], None
