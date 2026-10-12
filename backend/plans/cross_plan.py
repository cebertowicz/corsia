"""Tell a dependent's owner when a task it depends on settles or leaves (#1552, R10-R12).

A dependency may name a prerequisite in another thread's plan. The edge lives
on the dependent, so the prerequisite's own writers know nothing of it: they
reconcile only their own plan, and a failure or close leaves the dependent
blocked before and after, so no blocked delta reports it. This module is the
one place that closes that gap, inside the prerequisite's write:

- the dependent's plan is reconciled, so a dependent the outcome unblocked is
  selected at once, and its owner counts as told for that selection;
- the dependent's owner gets one wake per dependent, prerequisite and outcome,
  through ``plan_wake.append``, the one writer of plan wakes.

It is called from exactly two places: ``service._transition`` when a
prerequisite is accepted or fails for good (a rejection hands its dependents
to the revision instead, ``dependencies.move_dependents``, and they keep
waiting), and ``dependencies.drop_edges_for_node`` when a prerequisite is
closed or archived. A close drops the node's edges in the same write
(``archival.release_closed``), so its news is the edge going, told once there.

A dependent in the prerequisite's *own* plan hears only a failure or a close,
and only when someone other than that plan's owner caused it: the owner that
failed or closed its own task already has the result and its next block. A
done prerequisite needs no wake there, because the reconcile in the same write
selects the dependent and ``selection_wake.notify`` says so.
"""

from __future__ import annotations

from plans import plan_wake
from plans.node_states import _ACCEPTED_STATES, _TERMINAL_STATES, CLOSED, in_work_sql


def prerequisite_settled(connection, node_id: str, *, actor_id: str | None, provenance: str) -> None:
    """Tell each foreign dependent's owner that ``node_id`` was accepted or failed."""
    prerequisite = _prerequisite(connection, node_id)
    if prerequisite["state"] == CLOSED:
        return
    outcome = "done" if prerequisite["state"] in _ACCEPTED_STATES else "failed"
    _tell(
        connection, prerequisite, _dependents(connection, prerequisite),
        outcome=outcome, actor_id=actor_id, provenance=provenance,
    )
    if outcome == "failed":
        _tell_own_plan(
            connection, prerequisite,
            _dependents(connection, prerequisite, own_plan=True, not_owner=actor_id),
        )


def conductor_task_settled(connection, node_id: str, *, outcome: str) -> None:
    """Tell a conductor exactly once when its foreign-added task settles.

    The creation event's provenance is the durable capability receipt. Foreign
    actors also create send-back worker steps, so actor identity alone is not
    authority to notify.
    """
    from plans import closure, review_evidence, service
    from plans.close_next_bounds import bounded
    from plans.operator_node_facts import executor_labels, revision_root
    from prompts import plan_wakes as text

    node = _prerequisite(connection, node_id)
    # A send-back closes the added task without telling anyone and continues
    # it as a revision, so the receipt is read from the start of that chain.
    created = connection.execute(
        """SELECT actor_id FROM plan_node_events
           WHERE node_id=? AND event='node_created' AND provenance='plan.conductor_add'""",
        (revision_root(node_id, db=connection),),
    ).fetchone()
    if created is None or created["actor_id"] is None or created["actor_id"] == node["owner_id"]:
        return
    recipient = str(created["actor_id"])
    if connection.execute("SELECT 1 FROM threads WHERE id=?", (recipient,)).fetchone() is None:
        return
    recipient_plan = service.ensure_plan("thread", recipient, db=connection)
    reason = closure.settled_reasons(connection, [node]).get(str(node_id))
    evidence = review_evidence.latest(
        node_id, requirements_revision=int(node["requirements_revision"]), db=connection,
    )
    detail = reason or (None if evidence is None else str(evidence["evidence"])) or text.CLOSE_REASON_MISSING
    receiver = executor_labels({str(node["owner_id"])}, db=connection).get(
        str(node["owner_id"]), "Untitled thread",
    )
    line = text.WAKE_CONDUCTOR_TASK_SETTLED.format(
        title=str(node["title"]), receiver=receiver,
        outcome=text.CONDUCTOR_TASK_OUTCOMES.get(outcome, outcome),
        detail=bounded(detail, 480),
    )
    plan_wake.append(
        connection,
        recipient=recipient,
        kind=plan_wake.CONDUCTOR_TASK_SETTLED,
        subject_id=node_id,
        revision=int(node["revision"]),
        about=outcome,
        plan_id=recipient_plan.id,
        content=plan_wake.with_next(connection, recipient_plan.id, recipient, line),
    )


def dependents_of(connection, node_id: str) -> list:
    """The open foreign dependents waiting on a node, read before its edges drop."""
    prerequisite = _prerequisite(connection, node_id)
    if prerequisite["state"] in _ACCEPTED_STATES:
        # A met edge released its dependent when the prerequisite was accepted,
        # and that was already told; leaving the plan afterwards changes nothing.
        return []
    return _dependents(connection, prerequisite)


def own_dependents_of(connection, node_id: str, *, actor_id: str | None) -> list:
    """The open dependents in a closed node's own plan whose owner did not close it.

    Read before the edges drop, like :func:`dependents_of`. An archive is a
    retirement, not an outcome, and tells nobody in its own plan.
    """
    prerequisite = _prerequisite(connection, node_id)
    if prerequisite["state"] != CLOSED:
        return []
    return _dependents(connection, prerequisite, own_plan=True, not_owner=actor_id)


def told_owner(own_dependents: list) -> str | None:
    """The owner a same-plan close wake goes to, so the reconcile does not tell it twice."""
    return str(own_dependents[0]["owner_id"]) if own_dependents else None


def prerequisite_removed(
    connection, node_id: str, dependents: list, *, actor_id: str | None, provenance: str,
    own_dependents: list | None = None,
) -> None:
    """Tell each dependent read by :func:`dependents_of` or :func:`own_dependents_of`."""
    if dependents:
        prerequisite = _prerequisite(connection, node_id)
        _tell(
            connection, prerequisite, dependents,
            outcome="closed" if prerequisite["state"] == CLOSED else "archived",
            actor_id=actor_id, provenance=provenance,
        )
    if own_dependents:
        _tell_own_plan(connection, _prerequisite(connection, node_id), own_dependents)


def _prerequisite(connection, node_id: str):
    return connection.execute(
        """SELECT n.id,n.title,n.state,n.revision,n.requirements_revision,n.plan_id,pl.owner_kind,pl.owner_id
           FROM plan_nodes n JOIN plans pl ON pl.id=n.plan_id WHERE n.id=?""",
        (node_id,),
    ).fetchone()


def _dependents(
    connection, prerequisite, *, own_plan: bool = False, not_owner: str | None = None,
) -> list:
    """Open thread-owned dependents in other plans, or with ``own_plan`` in the
    prerequisite's own plan when that plan's owner is not ``not_owner``.

    CROSS JOIN pins the join order to the prerequisite's dependency edges: with
    ``own_plan`` the planner otherwise drives from every node in the plan,
    settled history included."""
    return connection.execute(
        f"""SELECT n.id,n.title,n.plan_id,pl.owner_id FROM plan_node_dependencies d
            CROSS JOIN plan_nodes n ON n.id=d.node_id CROSS JOIN plans pl ON pl.id=n.plan_id
            WHERE d.depends_on_node_id=? AND n.plan_id{'=' if own_plan else '<>'}?
              AND {in_work_sql('n')} AND pl.owner_kind='thread'
              AND (? OR pl.owner_id IS NOT ?)
              AND n.state NOT IN ({",".join("?" for _ in _TERMINAL_STATES)})
            ORDER BY n.plan_id,n.order_key,n.id""",
        (
            prerequisite["id"], prerequisite["plan_id"], not own_plan, not_owner,
            *sorted(_TERMINAL_STATES),
        ),
    ).fetchall()


def _tell(connection, prerequisite, dependents, *, outcome: str, actor_id, provenance) -> None:
    from plans import admission
    from plans.operator_node_facts import executor_labels

    owner_id = prerequisite["owner_id"] if prerequisite["owner_kind"] == "thread" else None
    owner = (
        executor_labels({owner_id}, db=connection).get(owner_id, "Untitled thread")
        if owner_id else "the project"
    )
    reconciled: set[str] = set()
    for dependent in dependents:
        plan_id, recipient = str(dependent["plan_id"]), str(dependent["owner_id"])
        if plan_id not in reconciled:
            reconciled.add(plan_id)
            # The owner hears this outcome below, with what is now current, so
            # it is the causing executor here and gets one message, not two.
            admission.reconcile_plan(
                plan_id, actor_id=actor_id, provenance=provenance,
                causing_executor=recipient, db=connection,
            )
        _notify(
            connection, recipient=recipient, plan_id=plan_id, dependent=dependent,
            prerequisite=prerequisite, owner=owner, outcome=outcome,
        )


def _tell_own_plan(connection, prerequisite, dependents) -> None:
    """Tell a dependent's owner that a task in its own plan failed or was closed.

    One wake per dependent, prerequisite and outcome, under the same identity
    rule as a foreign one. A close names its reason: that is how it ended.
    """
    from plans import closure
    from prompts import plan_wakes as text

    outcome = "closed" if prerequisite["state"] == CLOSED else "failed"
    reason = closure.settled_reasons(connection, [prerequisite]).get(str(prerequisite["id"]))
    for dependent in dependents:
        plan_id, recipient = str(dependent["plan_id"]), str(dependent["owner_id"])
        line = text.PREREQUISITE_OWN_PLAN_LINES[outcome].format(
            prerequisite=str(prerequisite["title"]), dependent=str(dependent["title"]),
            reason=reason or text.CLOSE_REASON_MISSING,
        )
        plan_wake.append(
            connection,
            recipient=recipient,
            kind=plan_wake.PREREQUISITE_SETTLED,
            subject_id=str(dependent["id"]),
            revision=int(prerequisite["revision"]),
            about=f"{prerequisite['id']}:{outcome}",
            open_node_id=str(dependent["id"]),
            plan_id=plan_id,
            content=plan_wake.with_next(
                connection, plan_id, recipient, line,
                prerequisite={"node_id": str(prerequisite["id"]), "outcome": outcome},
            ),
        )


def _notify(
    db, *, recipient: str, plan_id: str, dependent, prerequisite, owner: str, outcome: str,
) -> str | None:
    """Tell a dependent's owner a task it waits on in another plan settled or left.

    ``outcome`` is ``done``, ``failed`` (the edge stays, never met), or
    ``closed``/``archived`` (the edge went with it). One input per dependent,
    prerequisite and outcome (``plans.cross_plan``).
    """
    from prompts import plan_wakes as text

    meaning = text.PREREQUISITE_OUTCOMES.get(outcome, text.PREREQUISITE_OUTCOMES["removed"])
    line = text.WAKE_PREREQUISITE_SETTLED.format(
        prerequisite=str(prerequisite["title"]),
        owner=owner,
        outcome=meaning.format(dependent=str(dependent["title"]), how=outcome),
    )
    return plan_wake.append(
        db,
        recipient=recipient,
        kind=plan_wake.PREREQUISITE_SETTLED,
        subject_id=str(dependent["id"]),
        revision=int(prerequisite["revision"]),
        about=f"{prerequisite['id']}:{outcome}",
        open_node_id=str(dependent["id"]),
        plan_id=plan_id,
        content=plan_wake.with_next(
            db, plan_id, recipient, line,
            prerequisite={"node_id": str(prerequisite["id"]), "outcome": outcome},
        ),
    )
