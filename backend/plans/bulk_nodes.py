"""Change or close many named nodes in one call, and report each one's outcome.

Both verbs here copy the shape ``archive_node`` already ships
(``plans.archival.archive_nodes`` and ``plans.boundary_after.archive_many``),
because that is the shape an agent already knows and the one the Later design
names as the template (section 5):

* one connection and one write, so two hundred tasks cross the cut together;
* each id through the same single-node function, with identical guards and audit,
  in the caller's order -- which is what makes the obvious retry work, and lets a
  caller order a child before its parent so one call cleans a whole branch;
* refusals collected, never fatal: a caller that named forty tasks it has decided
  about should not lose thirty-nine because one still has an open child;
* lane authorization judged per node, since the lane rule is the boundary's and
  one lane-owned row is a fact about that row;
* no ``expected_revision``, because there is no single node the call is computed
  against -- the caller's ``mutation_id`` is what makes a retry safe instead;
* the executor's slot released and the plan reconciled once at the end, so one
  call starts at most one next task.

A bulk close deliberately does not wake the plan's owner once per closed task: it
is the owner cleaning its own plan, and a wake per row would be a message about a
fact the reply already carries. That is the choice a bulk archive already makes.
"""

from __future__ import annotations

from typing import Any, Callable

import plans.bulk_fields as bulk_fields
import plans.service as service


def dispatch(plan_id, operation, params, common, db, actor) -> dict:
    """Drive one verb across many named ids, in the caller's order.

    Nothing is relaxed for a list: each id goes through the same single-node
    writer with that verb's guards and audit intact, and every refusal is
    reported instead of losing the rest of the call.

    The field reader is imported lazily: it reads the caller's payload at the
    boundary, this module writes the rows, and the boundary already imports this
    one to route here.
    """
    from plans import boundary_fields

    acting = actor.thread_id if actor.kind == "thread" else None
    if operation == "edit":
        return edit_many(
            plan_id, params["node_ids"], bulk_fields.edit_body(params, boundary_fields.text),
            common, db, acting=acting,
        )
    return close_many(
        plan_id, params["node_ids"], boundary_fields.text(params, "reason"), common, db,
        acting=acting,
    )


def edit_many(
    plan_id: str, node_ids: list[str], changed: dict[str, Any], common: dict, db,
    *, acting: str | None,
) -> dict:
    """Apply one Later-or-branch change to many nodes, in the caller's order.

    Returns:
        ``{"changed": [{"node_id", "revision"}], "refused": [{"node_id", "reason"}]}``,
        each in the caller's order, plus ``next`` when the call freed a slot.
    """
    from plans import mutations

    held = _held_slots(db, node_ids)
    applied, reasons = _apply(db, node_ids, "edit", lambda node_id: mutations.edit_node(
        node_id, **changed, **common,
    ))
    return {
        "changed": applied,
        "refused": _refused(node_ids, reasons),
        **_free_slot(plan_id, held, applied, common, acting, closed=False),
    }


def close_many(
    plan_id: str, node_ids: list[str], reason: str, common: dict, db,
    *, acting: str | None,
) -> dict:
    """Close many nodes as no longer wanted, in the caller's order.

    Returns:
        ``{"closed": [{"node_id", "revision"}], "refused": [{"node_id", "reason"}]}``,
        each in the caller's order, plus ``next`` when the call freed a slot.
    """
    from plans import closure
    from plans.workflow_fields import refuse_required_skip

    held = _held_slots(db, node_ids)

    def close_one(node_id: str):
        # The same guard the single close runs, per node: a workflow run's required
        # step is never skipped in bulk either. A required step that closes without
        # being done also fails its run (``plans.run_end``), so refusing it here is
        # what keeps a bulk cleanup from quietly ending a run.
        refuse_required_skip(db, node_id)
        return closure.close_node(node_id, reason=reason, **common)

    applied, reasons = _apply(db, node_ids, "close_node", close_one)
    return {
        "closed": applied,
        "refused": _refused(node_ids, reasons),
        **_free_slot(plan_id, held, applied, common, acting, closed=True),
    }


def _apply(
    db, node_ids, operation: str, one: Callable[[str], Any],
) -> tuple[list[dict], dict[str, str]]:
    """Run one node's write for every id this boundary may drive.

    Returns:
        The applied entries (``node_id`` and the revision the write left) and the
        refusal text by id. Both are judged per node: a lane owning one row, or one
        row's own guard, is not a reason to discard the rest of the list.
    """
    import plans.node_settlement as node_settlement

    applied: list[dict] = []
    reasons: dict[str, str] = {}
    for node_id in dict.fromkeys(node_ids):
        lane = node_settlement.lane_refusing(db, node_id, operation)
        if lane is not None:
            reasons[node_id] = node_settlement.refusal_message(lane, operation)
            continue
        try:
            node = one(node_id)
        except service.PlanError as refusal:
            # Every domain refusal is one row's fact. Each node's write is its own
            # savepoint inside this call's transaction, so a refusal takes back that
            # row and nothing else -- which is why a refused id keeps the revision
            # it had and can be retried on its own.
            reasons[node_id] = str(refusal)
        else:
            applied.append({"node_id": str(node.id), "revision": int(node.revision)})
    return applied, reasons


def _refused(node_ids, reasons: dict[str, str]) -> list[dict]:
    """Every refusal, once each, in the caller's order."""
    return [
        {"node_id": node_id, "reason": reasons[node_id]}
        for node_id in dict.fromkeys(node_ids) if node_id in reasons
    ]


def _held_slots(db, node_ids) -> dict:
    """The slot each named task holds right now, for the ids that hold one.

    Read before any of this touches the plan: a release reports the executor and
    the holder as they were, which is what ``close_next.slot_before`` records.
    """
    import plans.admission_attempts as attempts
    from plans import close_next

    held = {}
    for node_id in dict.fromkeys(node_ids):
        if attempts.open_attempt(db, node_id) is not None:
            held[node_id] = close_next.slot_before(db, service._node_row(db, node_id))
    return held


def _free_slot(
    plan_id: str, held: dict, applied: list[dict], common: dict,
    acting: str | None, *, closed: bool,
) -> dict:
    """Release every slot this call took off a task, reconcile once, and report next.

    One reconciliation for the whole list: it reads the plan's current slots, so
    running it per node would repeat the same read and could start a task the rest
    of the list was about to make ineligible.
    """
    from plans import admission, close_next

    db = common["db"]
    done = {entry["node_id"] for entry in applied}
    reason = service.CLOSED if closed else "later"
    freed = [
        node_id for node_id in held
        if node_id in done and admission.release(node_id, reason, db=db)
    ]
    if not freed:
        return {}
    admission.reconcile_plan(
        plan_id, actor_id=common["actor_id"], provenance=common["provenance"],
        causing_executor=acting, db=db,
    )
    # A task moved below the cut stays open, so it is no closed task to leave out of
    # the listing; a closed one is, and its row is what the listing is read for.
    last = freed[-1]
    return close_next.next_fields(
        db, service._node_row(db, last) if closed else None, held[last], None, acting=acting,
    )
