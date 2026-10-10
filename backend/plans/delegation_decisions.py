"""A worker's own accountable blocker, and its drafted decision (B4, E1).

Split out of ``plans.delegation_notice`` to keep both files under the size
ratchet, matching ``plans.delegation_friction``'s precedent: the parent-notice
table and its digest/wake decision still live there; this module reaches back
into it for the one shared wake/digest primitives (``_wake_parent``,
``_append``) rather than keeping a second copy of either.

Both rows share one shape neither of the plain digests does: the worker
raised something only the parent can act on, so it wakes the parent instead
of a digest, and the worker itself is told, passively, to move on.
"""

from __future__ import annotations

import hashlib


def notify(
    connection, *, node_id: str, row, event: str, event_id: str,
    actor_id: str | None, payload: dict, created_at: str,
    parent_plan_id: str, worker: str,
) -> bool:
    """Route a B4 blocker or an E1 drafted decision to its wake.

    Returns whether this event was one of the two and was handled, so
    ``observe_node_event`` falls through to the ordinary digest table for
    everything else -- including a blocker that is the user's own, which is
    a hold and the user's to clear. ``row`` is the worker's ``plan_nodes``
    row that carries the event (``plan_id``, ``title``).
    """
    plan_id, title = str(row["plan_id"]), str(row["title"])
    blocked = payload.get("blocked") if event == "watch_armed" else None
    if blocked:
        _blocker_raised(
            connection, node_id=node_id, title=title, actor_id=actor_id,
            payload=payload, reason=str(blocked.get("reason") or ""),
            parent_plan_id=parent_plan_id, worker=worker,
            event_id=event_id, created_at=created_at,
        )
        return True
    if event == "decision_drafted":
        _decision_drafted(
            connection, node_id=node_id, plan_id=plan_id, event_id=event_id,
            actor_id=actor_id, payload=payload, created_at=created_at,
            parent_plan_id=parent_plan_id, worker=worker,
        )
        return True
    return False


#: The same blocker's earlier waits on a step, newest first (debt 100).
_SAME_BLOCKER_SQL = """
    SELECT w.status FROM plan_node_events e
      JOIN expected_event_watches w ON w.id=json_extract(e.payload_json,'$.watch_id')
     WHERE e.node_id=? AND e.event='watch_armed' AND w.id<>?
       AND json_extract(e.payload_json,'$.blocked.reason')=?
     ORDER BY e.created_at DESC, e.rowid DESC
"""


def _blocker_raised(
    connection, *, node_id: str, title: str, actor_id: str | None, payload: dict,
    reason: str, parent_plan_id: str, worker: str, event_id: str, created_at: str,
) -> None:
    """Wake the parent about a blocker the worker cannot clear itself (B4).

    Such a blocker is a wait the worker armed (``plans.blocked_wait``); only
    the user's own blocker is a hold, and that one is a digest. One blocker is
    the step and its reason (debt 100): arming it again while its last wait is
    armed or expired renews it, which the parent reads as "renewed until
    <date>" and is not woken for. A blocker that cleared and was raised again
    is new news and wakes again, so each clearing starts a new occurrence.
    A blocker on the parent itself (``to_parent``, N2) names how to answer it.
    """
    from plans import delegation_notice as notice

    earlier = [
        str(row["status"]) for row in connection.execute(
            _SAME_BLOCKER_SQL, (node_id, str(payload.get("watch_id")), reason),
        ).fetchall()
    ]
    if earlier and earlier[0] != "resolved":
        _blocker_renewed(
            connection, node_id=node_id, title=title, actor_id=actor_id,
            payload=payload, reason=reason, parent_plan_id=parent_plan_id,
            worker=worker, event_id=event_id, created_at=created_at,
        )
        return
    blocker = hashlib.sha256(f"{node_id}\n{reason}".encode()).hexdigest()[:16]
    notice._wake_parent(
        connection, parent_plan_id, worker,
        kind=("child_needs_input" if (payload.get("blocked") or {}).get("to_parent")
              else "child_blocker_raised"),
        subject_id=f"blocker-{blocker}",
        revision=1 + earlier.count("resolved"),
        actor_id=actor_id, title=title, reason=reason,
    )


def _blocker_renewed(
    connection, *, node_id: str, title: str, actor_id: str | None, payload: dict,
    reason: str, parent_plan_id: str, worker: str, event_id: str, created_at: str,
) -> None:
    """Tell the parent, passively, that its worker renewed a blocker.

    The parent's own act is its receipt, as it is for the wake above.
    """
    from plans import delegation_notice as notice, service

    parent = service._plan_row(connection, parent_plan_id)
    if actor_id == str(parent["owner_id"]):
        return
    until = notice._payload(payload.get("condition")).get("value")
    notice._append(
        connection,
        plan_id=parent_plan_id,
        event_id=event_id,
        actor_id=actor_id or worker,
        change_kind="plan.child_blocker_renewed",
        summary=f"{notice._STEP} blocker renewed until {until}: {title} — {reason}",
        details={
            "node_id": node_id, "watch_id": payload.get("watch_id"),
            "reason": reason, "worker_thread_id": worker,
        },
        created_at=created_at,
    )


def _decision_drafted(
    connection, *, node_id: str, plan_id: str, event_id: str,
    actor_id: str | None, payload: dict, created_at: str,
    parent_plan_id: str, worker: str,
) -> None:
    """Wake the parent with a child's drafted decision in full, and free the child.

    The parent tier alone may ``parent_review``/``raise`` a child's decision
    (``decision_authz.require_reviewer``, unchanged); this only carries the
    draft to the parent so it never has to fetch it, and tells the child --
    without waking it -- that the decision is now the parent's, so it is not
    left stalled on it (matrix row E1, settled call 2).
    """
    from plans import decision_projection
    from plans import delegation_notice as notice
    from prompts import plan_wakes as text

    decision_id = str(payload.get("decision_id") or "")
    decision = decision_projection.read(decision_id, db=connection)
    notice._wake_parent(
        connection, parent_plan_id, worker,
        kind="child_decision_drafted", subject_id=decision_id, revision=1,
        actor_id=actor_id, decision_title=decision["title"],
        extra={
            "decision_id": decision_id,
            "title": decision["title"],
            "context": decision["context"],
            "tradeoff": decision["tradeoff"],
            "pick": decision["pick"],
            "recommendation": decision["recommendation"],
            "options": decision["options"],
        },
    )
    notice._append(
        connection,
        plan_id=plan_id,
        event_id=event_id,
        actor_id=actor_id or worker,
        change_kind="plan.decision_drafted_pending_parent",
        summary=(
            f"Decision drafted: {decision['title']} — with your parent now;"
            " pick up your next task"
        ),
        details={
            "decision_id": decision_id, "node_id": node_id,
            "chip": text.DECISION_DRAFTED_CHIP,
        },
        created_at=created_at,
    )
