"""A planless worker's final reply is its return (#282).

Split out of ``plans.delegation_harvest`` (at its size cap). The reply goes
through that module's one harvest writer, so the step, provenance, slot and
"has returned" wake are the ones every other return gets.
"""

from __future__ import annotations

from plans.delegation import _valid_structured_epoch
from plans.delegation_harvest import _harvest, _harvested, _latest_child_event


def harvest_reply(
    connection, *, step, reply: str, changed_plan_ids: list[str] | None = None,
) -> bool:
    """Harvest a worker that ended its Turn with a reply and no plan (#282).

    The reply is its return: the same event, review transition, slot and
    "has returned" wake as any harvest, carrying the reply as the evidence.
    False when the step has no structured attachment to harvest, was already
    harvested, or the parent cannot take the wake; the caller then falls back
    to the returned-nothing notice.
    """
    step = connection.execute("SELECT * FROM plan_nodes WHERE id=?", (step["id"],)).fetchone()
    epoch = _valid_structured_epoch(step, connection)
    if epoch is None:
        return False
    attachment, payload = epoch
    if _harvested(step["id"], attachment["id"], connection):
        return False
    child_event = _latest_child_event(payload["child_plan_id"], connection)
    if not _harvest(step, attachment, payload, child_event, connection, reply=reply):
        return False
    if changed_plan_ids is not None and step["plan_id"] not in changed_plan_ids:
        changed_plan_ids.append(step["plan_id"])
    return True


def reply_step(reply: str) -> dict:
    """The return a planless worker's final reply makes, shaped like a step."""
    from plans.close_next_bounds import bounded
    from plans.plan_wake import RETURNED_EVIDENCE_MAX_BYTES

    return {
        "id": None, "title": "final reply", "state": "replied",
        "evidence": bounded(reply, RETURNED_EVIDENCE_MAX_BYTES), "artifact_ids": [],
    }


def plan_is_empty(thread_id: str, *, db=None) -> bool:
    """Whether this thread's own plan holds no step: nothing for a return to carry."""
    from plans import node_states, service

    with service._connection(db) as connection:
        return connection.execute(
            f"""SELECT 1 FROM plan_nodes n JOIN plans p ON p.id=n.plan_id
               WHERE p.owner_kind='thread' AND p.owner_id=?
                 AND {node_states.unarchived_sql('n')} LIMIT 1""",
            (thread_id,),
        ).fetchone() is None
