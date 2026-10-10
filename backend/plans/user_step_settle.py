"""The plan owner settles a step assigned to the user that is really done.

Split from ``plans.boundary_dispatch``, whose ``mark_done`` hands a
user-assigned row here. The step finished outside its row (the user said so in
chat, or the owner checked), so the owner records the evidence and the row
reads Done with the owner as ``verified_by``; the pane shows
``Marked by <owner>`` (``plans.closure.settled_reasons``). Approvals stay
user-only: a decision's row is refused before it reaches here
(``plans.node_settlement``).
"""

from __future__ import annotations

from typing import Any

import plans.boundary_after as after
import plans.boundary_fields as fields
import plans.errors as errors
import plans.service as service
import plans.user_settle as user_settle

USER_PAUSED_STEP = (
    "the user paused this step and only they end it: leave it, or ask them to "
    "resume it or mark it done"
)


def mark_done(params: dict[str, Any], actor, common: dict, db) -> dict:
    """Accept a pending or active step assigned to the user on the owner's evidence.

    A user step is never selectable, so this never takes the executor slot; it
    settles through the writers the user's own Mark done uses. A row with open
    children or one the user paused is refused, because only the user ends what
    they paused. Its other stops (a blocker, an unmet prerequisite, a watch)
    end with it, as on the user's own Mark done: the step is done.
    """
    from plans import archival, boundary_dispatch, holds

    node_id = params["node_id"]
    node = service._node_row(db, node_id)
    state = node["state"]
    if state == "review_requested":
        raise service.PlanError(boundary_dispatch.MARK_DONE_REVIEW)
    if state not in {"pending", "active"}:
        raise service.PlanError(
            f"mark_done settles pending or active work; this task is {state}"
        )
    archival._require_no_open_children(
        db, node_id,
        "mark_done would strand open children: settle or close them first",
    )
    if any(
        hold["kind"] == "paused" and hold["owner_kind"] == "user"
        for hold in holds.active_holds(node_id, db=db)
    ):
        raise service.PlanError(USER_PAUSED_STEP)
    fields.require_passed_verification(params)
    errors.require_node_revision(node, params["expected_revision"])
    user_settle.clear_stops(db, node_id, common)
    if state == "pending":
        service.transition_node(
            node_id, "active",
            expected_revision=service._node_row(db, node_id)["revision"], **common,
        )
    params = {**params, "expected_revision": service._node_row(db, node_id)["revision"]}
    expected = boundary_dispatch._submit_finished_work(
        node_id, params, common, actor.thread_id, db,
    )
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by=actor.audit_id, **expected, **common,
    )
    return after.after_close(
        node_id, "accepted", params, common, executor=actor.thread_id,
    )
