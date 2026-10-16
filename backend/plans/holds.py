"""Append-only paused and blocked causes for one plan task."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping

import plans.errors as errors
import plans.service as service
from plans.blocked_wait import WAIT_OWNERS

HOLD_KINDS = frozenset({"paused", "blocked"})
#: A hold names a real wait, and a task waits before it starts and while its
#: review is outstanding as truthfully as it does mid-execution.
_HELDABLE_STATES = frozenset({"pending", "active", "review_requested"})
OWNER_KINDS = frozenset({"user", "thread", "external", "system"})


def _row(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "reason_key": row["reason_key"],
        "reason": row["reason"],
        "owner_kind": row["owner_kind"],
        "owner_id": row["owner_id"],
        "created_at": row["created_at"],
    }


def active_holds(node_id: str, *, db) -> list[dict]:
    """Return every uncleared cause in deterministic precedence order."""

    rows = db.execute(
        """SELECT * FROM plan_node_holds
           WHERE node_id=? AND cleared_at IS NULL
           ORDER BY CASE kind WHEN 'paused' THEN 0 ELSE 1 END,created_at,id""",
        (node_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def _validate_node(db, node_id: str, expected_revision: int):
    node = service._live_node_row(db, node_id)
    errors.require_node_revision(node, expected_revision)
    if node["kind"] != "task" or node["state"] not in _HELDABLE_STATES:
        raise service.PlanError("holds require an open task")
    return node


def place_hold(
    node_id: str,
    *,
    kind: str,
    reason_key: str,
    reason: str,
    owner_kind: str,
    owner_id: str | None,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
    raised_in: Mapping[str, str] | None = None,
    _validated_node=None,
):
    """Append one cause and release its selected attempt; the caller reconciles.

    ``raised_in`` names where an agent raised a user ask (``turn_id``,
    ``tool_invocation_id``, ``provider_call_id``); it rides nested under
    ``raised_in`` in the ``hold_placed`` payload, so no key can shadow the event's
    own fields, and the chat shows the ask on the step that raised it.

    Returns:
        The task row as it was before the cause was placed.
    """

    node = _validated_node or _validate_node(db, node_id, expected_revision)
    if kind not in HOLD_KINDS:
        raise service.PlanInputError("invalid plan hold kind")
    if owner_kind not in OWNER_KINDS:
        raise service.PlanInputError("invalid plan hold owner kind")
    reason_key = service._required_text(reason_key, "hold reason key")
    reason = service._required_text(reason, "hold reason")
    if owner_id is not None:
        owner_id = service._required_text(owner_id, "hold owner id")
    if kind == "paused":
        existing = db.execute(
            """SELECT id FROM plan_node_holds
               WHERE node_id=? AND kind='paused' AND cleared_at IS NULL""",
            (node_id,),
        ).fetchone()
        if existing is not None:
            raise service.PlanError("task already has an active pause hold")
    hold_id = uuid.uuid4().hex
    now = service._now()
    db.execute(
        """INSERT INTO plan_node_holds
           (id,node_id,kind,reason_key,reason,owner_kind,owner_id,
            created_by_actor_id,provenance,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            hold_id, node_id, kind, reason_key, reason, owner_kind, owner_id,
            actor_id, service._required_text(provenance, "provenance"), now,
        ),
    )
    service._event(
        db,
        node_id,
        "hold_placed",
        actor_id=actor_id,
        provenance=provenance,
        payload={
            "hold_id": hold_id,
            "kind": kind,
            "reason_key": reason_key,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            **({"raised_in": dict(raised_in)} if raised_in else {}),
        },
    )
    service.touch_node(db, node_id, at=now)
    from plans import admission

    admission.release(node_id, kind, db=db)
    return node


def _reconciled(node, *, actor_id: str | None, provenance: str, db) -> dict:
    """Reconcile a task's plan without preemption and return the task's projection."""
    from plans import admission

    admission.reconcile_plan(
        node["plan_id"], actor_id=actor_id, provenance=provenance, db=db,
    )
    return admission.result(node["id"], db=db)


def clear(
    node_id: str,
    hold_id: str,
    *,
    expected_kind: str,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    causing_executor: str | None = None,
    db,
) -> dict:
    """Clear exactly one cause and reconcile the plan without preemption.

    ``causing_executor`` is the thread whose own write this is, when one is
    acting: clearing a cause reselects the task, and an executor resuming its
    own held work already knows, so it is never woken for it.
    """

    node = _validate_node(db, node_id, expected_revision)
    hold = db.execute(
        """SELECT h.owner_id FROM plan_node_holds h
           JOIN plan_node_artifact_links l
             ON l.node_id=h.node_id AND l.artifact_id=h.owner_id AND l.role='attention'
           WHERE h.id=? AND h.node_id=? AND h.kind=? AND h.owner_kind='user'
             AND h.cleared_at IS NULL""",
        (hold_id, node_id, expected_kind),
    ).fetchone()
    if expected_kind == "blocked" and hold is not None and hold["owner_id"] is not None:
        from plans.boundary import is_user_audit_id

        clear_write(
            node_id, hold_id, expected_kind=expected_kind, actor_id=actor_id,
            provenance=provenance,
            detail=None if is_user_audit_id(actor_id) else {"ended": "withdrawn"},
            db=db,
        )
    else:
        clear_write(
            node_id, hold_id, expected_kind=expected_kind, actor_id=actor_id,
            provenance=provenance, db=db,
        )
    from plans import admission

    admission.reconcile_plan(
        node["plan_id"], actor_id=actor_id, provenance=provenance,
        causing_executor=causing_executor, db=db,
    )
    return admission.result(node_id, db=db)


def clear_write(
    node_id: str, hold_id: str, *, expected_kind: str, actor_id: str | None,
    provenance: str, db, detail: dict | None = None,
) -> tuple[str, str]:
    """Clear exactly one cause and record it; the caller reconciles, if needed.

    ``detail`` rides in the append-only clearance event under its own ``detail`` key,
    for what the clearing decided (a review verdict); it can never shadow the event's fields.
    Return the clearance event's id and timestamp so a caller can report that
    exact durable event without rereading it.
    """
    hold = db.execute(
        """SELECT * FROM plan_node_holds
           WHERE id=? AND node_id=? AND kind=? AND cleared_at IS NULL""",
        (hold_id, node_id, expected_kind),
    ).fetchone()
    if hold is None:
        raise service.PlanError("plan hold is unavailable or already cleared")
    payload: dict = {"hold_id": hold_id, "kind": expected_kind}
    if detail is not None:
        payload["detail"] = detail
    event_id = service._event(
        db,
        node_id,
        "hold_cleared",
        actor_id=actor_id,
        provenance=provenance,
        payload=payload,
    )
    now = service._now()
    changed = db.execute(
        """UPDATE plan_node_holds
           SET cleared_by_actor_id=?,cleared_at=?,clearance_event_id=?
           WHERE id=? AND cleared_at IS NULL""",
        (actor_id or "system", now, event_id, hold_id),
    )
    if changed.rowcount != 1:
        raise service.PlanError("plan hold clearance conflict")
    service.touch_node(db, node_id, at=now)
    return event_id, now


def pause_hold(
    node_id: str,
    reason: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
):
    """Place a manual pause hold owned by the pausing actor; the caller reconciles.

    The hold's ``owner_kind`` says whose pause it is: ``user`` when the user
    paused, ``thread`` for an agent. ``owner_id`` is the actor's audit id.
    """
    from plans.boundary import is_user_audit_id

    return place_hold(
        node_id,
        kind="paused",
        reason_key="manual_pause",
        reason=reason,
        owner_kind="user" if is_user_audit_id(actor_id) else "thread",
        owner_id=actor_id,
        expected_revision=expected_revision,
        actor_id=actor_id,
        provenance=provenance,
        db=db,
    )


def pause(
    node_id: str,
    reason: str,
    *,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
) -> dict:
    """Place a manual pause hold owned by the pausing actor and reconcile the plan."""
    node = pause_hold(
        node_id, reason, expected_revision=expected_revision,
        actor_id=actor_id, provenance=provenance, db=db,
    )
    return _reconciled(node, actor_id=actor_id, provenance=provenance, db=db)


def block_hold(
    node_id: str,
    *,
    reason_key: str,
    reason: str,
    owner_kind: str,
    owner_id: str | None,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
    raised_in: Mapping[str, str] | None = None,
):
    """Place a blocker hold with its accountable owner; the caller reconciles.

    Only the user's own blocker is a hold: one anyone else clears is a wait
    with a deadline (``plans.blocked_wait``), so it never sits unlooked-at.
    Re-blocking a task on the artifact it already asks about keeps one open
    hold for that ask: the same reason writes nothing. A new reason atomically
    replaces the old ask, so the user sees one current request and the caller's
    revision is checked once before either write.
    """
    if owner_kind in WAIT_OWNERS:
        raise service.PlanInputError(
            "a blocker anyone but the user clears is a wait, not a hold; "
            "block_node arms it with a deadline"
        )
    if owner_kind == "user" and owner_id is not None:
        node = _validate_node(db, node_id, expected_revision)
        # Served by ``idx_plan_holds_active_node``: it reads only this task's
        # open blocked holds, never its cleared history.
        existing = db.execute(
            """SELECT id,reason_key,reason FROM plan_node_holds
               WHERE node_id=? AND kind='blocked' AND owner_kind='user'
                 AND owner_id=? AND cleared_at IS NULL""",
            (node_id, owner_id),
        ).fetchone()
        if existing is not None:
            if (existing["reason_key"], existing["reason"]) == (reason_key, reason):
                return node
            clear_write(
                node_id, str(existing["id"]), expected_kind="blocked", actor_id=actor_id,
                provenance=provenance, detail={"ended": "replaced"}, db=db,
            )
            return place_hold(
                node_id, kind="blocked", reason_key=reason_key, reason=reason,
                owner_kind=owner_kind, owner_id=owner_id,
                expected_revision=expected_revision, actor_id=actor_id,
                provenance=provenance, db=db, raised_in=raised_in,
                _validated_node=node,
            )
    return place_hold(
        node_id,
        kind="blocked",
        reason_key=reason_key,
        reason=reason,
        owner_kind=owner_kind,
        owner_id=owner_id,
        expected_revision=expected_revision,
        actor_id=actor_id,
        provenance=provenance,
        db=db,
        raised_in=raised_in,
    )


def block(
    node_id: str,
    *,
    reason_key: str,
    reason: str,
    owner_kind: str,
    owner_id: str | None,
    expected_revision: int,
    actor_id: str | None,
    provenance: str,
    db,
) -> dict:
    """Place a blocker hold with its accountable owner and reconcile the plan."""
    node = block_hold(
        node_id, reason_key=reason_key, reason=reason, owner_kind=owner_kind,
        owner_id=owner_id, expected_revision=expected_revision,
        actor_id=actor_id, provenance=provenance, db=db,
    )
    return _reconciled(node, actor_id=actor_id, provenance=provenance, db=db)
