"""Append-only plan-level audit events, the plan-scoped twin of node events."""

from __future__ import annotations

import json


_PLAN_TRANSITIONS = {
    "paused": ("plan.paused", "Plan paused."),
    "active": ("plan.resumed", "Plan resumed."),
}
_MILESTONE_TRANSITIONS = {
    "accepted": ("plan.milestone_accepted", "Top-level milestone accepted"),
    "accepted_with_followup": (
        "plan.milestone_accepted_with_followup",
        "Top-level milestone accepted with follow-up",
    ),
    "failed": ("plan.milestone_failed", "Top-level milestone failed"),
}

#: The provenance every schema step writes under (``migration.<from>_to_<to>``).
SCHEMA_STEP = "migration."


def from_schema_step(provenance: str) -> bool:
    """Whether an event only restates existing state in a new shape.

    A schema step moves old data through the runtime's own writers, but
    nothing happened: no owner or parent is told or woken, since the event
    is the upgrade's and not anyone's news.
    """
    return provenance.startswith(SCHEMA_STEP)


def _thread_owner(connection, plan_id: str) -> str | None:
    row = connection.execute(
        "SELECT p.owner_kind,p.owner_id,s.lifecycle_state,s.archived_at "
        "FROM plans p LEFT JOIN threads s ON s.id=p.owner_id WHERE p.id=?",
        (plan_id,),
    ).fetchone()
    if (
        row is None
        or row["owner_kind"] != "thread"
        or row["lifecycle_state"] != "active"
        or row["archived_at"] is not None
    ):
        return None
    return str(row["owner_id"])


def append_owner_event(
    connection,
    *,
    plan_id: str,
    event_id: str,
    actor_id: str | None,
    event_type: str,
    summary: str,
    details: dict,
    created_at: str,
) -> str | None:
    """Append one receipt or passive input at the event's actual boundary.

    The one rule for reaching a plan's owner: what the owner itself did is a
    receipt in its transcript, and what anyone else did is a passive change it
    reads at its next boundary. Both the plan's own events and the notices
    ``plans.delegation_notice`` sends a parent go through here, so neither can
    drift into waking a thread that should only have been told.
    """
    owner = _thread_owner(connection, plan_id)
    if owner is None:
        return None
    is_receipt = actor_id in {None, owner}
    if not is_receipt:
        from infra.passive_changes import append_change

        append_change(
            connection,
            object_kind="plan",
            object_id=plan_id,
            change_kind=event_type,
            summary=summary,
            details={**details, "source_event_id": event_id},
            actor_thread_id=actor_id,
            created_at=created_at,
        )
        return None
    # A receipt row's chip line is ``metadata.summary``, as a wake row's is;
    # the agent reads the message content either way.
    chip = details.get("chip")
    metadata = {
        "type": "activity_receipt",
        "event_type": event_type,
        "origin": "plan",
        "plan_id": plan_id,
        "source_event_id": event_id,
        **{key: value for key, value in details.items() if key != "chip"},
        "summary": chip if isinstance(chip, str) and chip else summary,
    }
    message_id = f"plan-event:{event_id}"
    from infra.queries import insert_message

    insert_message(
        owner, "system", summary, metadata,
        msg_id=message_id, db=connection, created_at=created_at,
    )
    return message_id


def append_worker_replaced(
    connection,
    *,
    worker_id: str,
    node_id: str,
    replacement_id: str,
    actor_id: str | None,
    created_at: str,
) -> None:
    """Tell a live worker its step was handed to another: one passive digest.

    Always a digest, never a receipt or a wake: the worker reads it at its next
    boundary and is not started by it. A worker that is gone or archived has
    nobody to tell, so nothing is written.
    """
    from infra.passive_changes import append_change
    from prompts import plan_wakes as text

    plan = connection.execute(
        "SELECT id FROM plans WHERE owner_kind='thread' AND owner_id=?",
        (worker_id,),
    ).fetchone()
    node = connection.execute(
        "SELECT title FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()
    if plan is None or node is None or _thread_owner(connection, str(plan["id"])) is None:
        return
    append_change(
        connection,
        object_kind="plan",
        object_id=str(plan["id"]),
        change_kind="plan.worker_replaced",
        summary=text.WORKER_REPLACED.format(
            step=node["title"], replacement=replacement_id,
        ),
        details={
            "node_id": node_id, "replacement": replacement_id,
            "chip": text.WORKER_REPLACED_CHIP,
        },
        actor_thread_id=actor_id,
        created_at=created_at,
    )


def append_plan_transition(
    connection,
    *,
    plan_id: str,
    event_id: str,
    actor_id: str | None,
    prior_state: str | None,
    new_state: str | None,
    created_at: str,
) -> str | None:
    """Project a committed whole-plan pause or resume once."""
    from plans import delegation_notice

    delegation_notice.observe_plan_transition(
        connection, plan_id=plan_id, event_id=event_id, actor_id=actor_id,
        new_state=new_state, created_at=created_at,
    )
    transition = _PLAN_TRANSITIONS.get(str(new_state or ""))
    if transition is None or (new_state == "active" and prior_state != "paused"):
        return None
    event_type, summary = transition
    return append_owner_event(
        connection,
        plan_id=plan_id,
        event_id=event_id,
        actor_id=actor_id,
        event_type=event_type,
        summary=summary,
        details={},
        created_at=created_at,
    )


def append_node_transition(
    connection,
    *,
    node_id: str,
    event_id: str,
    actor_id: str | None,
    new_state: str | None,
    created_at: str,
) -> str | None:
    """Project a terminal top-level milestone to its own plan's owner.

    Nested milestones stay quiet. What the plan that *delegated* this work
    hears is not this projection's business: ``plans.delegation_notice`` owns
    every parent notice, reached from ``service._event`` for every node event
    rather than only the terminal ones.
    """
    if str(new_state or "") not in _MILESTONE_TRANSITIONS:
        return None
    row = connection.execute(
        "SELECT n.plan_id,n.parent_id,n.title FROM plan_nodes n WHERE n.id=?",
        (node_id,),
    ).fetchone()
    if row is None or row["parent_id"] is not None:
        return None
    event_type, label = _MILESTONE_TRANSITIONS[str(new_state)]
    summary = f"{label}: {row['title']}"
    return append_owner_event(
        connection,
        plan_id=str(row["plan_id"]),
        event_id=event_id,
        actor_id=actor_id,
        event_type=event_type,
        summary=summary,
        details={"node_id": node_id},
        created_at=created_at,
    )


def project_node_event(
    connection,
    *,
    node_id: str,
    event: str,
    event_id: str,
    actor_id: str | None,
    new_state: str | None,
    payload,
    provenance: str,
    created_at: str,
) -> None:
    """Project one committed node event to everyone who should learn of it.

    The one fan-out for a plan node event, so ``plans.service._event`` stays a
    writer and the question "who hears about this?" has one answer in one
    place. Each projector decides whether the event is its business: the
    node's own plan owner hears a terminal top-level milestone, and the plan
    that delegated the work hears what its worker did. A schema step's
    event is no one's news (``from_schema_step``).
    """
    from plans import delegation_notice, update_cards

    if from_schema_step(provenance):
        return

    append_node_transition(
        connection, node_id=node_id, event_id=event_id, actor_id=actor_id,
        new_state=new_state, created_at=created_at,
    )
    update_cards.observe_node_event(
        connection, node_id=node_id, event=event, event_id=event_id,
        new_state=new_state, payload=payload, created_at=created_at,
    )
    delegation_notice.observe_node_event(
        connection, node_id=node_id, event=event, event_id=event_id,
        actor_id=actor_id, new_state=new_state, payload=payload,
        created_at=created_at,
    )


def append(
    connection,
    plan_id: str,
    event: str,
    *,
    actor_id: str | None,
    provenance: str,
    prior_state: str | None = None,
    new_state: str | None = None,
    payload: dict | None = None,
) -> str:
    """Append one plan-level audit event inside the surrounding transaction.

    The plan-scoped sibling of ``plans.service._event``: same append-only,
    revision-agnostic shape, but targets ``plan_events`` (no owning node) for
    mutations with nothing to attach to — currently only whole-plan lifecycle
    transitions. Reuses ``service``'s id/timestamp/validation helpers rather
    than duplicating them, per this package's existing intra-module
    convention (``plans.lifecycle`` already reaches into the same helpers).
    """
    from plans import service

    event_id = service._id()
    created_at = service._now()
    connection.execute(
        "INSERT INTO plan_events "
        "(id,plan_id,actor_id,provenance,event,prior_state,new_state,"
        "payload_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            event_id, plan_id, actor_id,
            service._required_text(provenance, "provenance"), event,
            prior_state, new_state,
            json.dumps(payload, sort_keys=True) if payload else None,
            created_at,
        ),
    )
    if event == "state_transition" and not from_schema_step(provenance):
        append_plan_transition(
            connection,
            plan_id=plan_id,
            event_id=event_id,
            actor_id=actor_id,
            prior_state=prior_state,
            new_state=new_state,
            created_at=created_at,
        )
    return event_id
