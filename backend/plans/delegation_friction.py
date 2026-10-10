"""One child step's own friction: repeated failure the parent may need to act on.

Split out of ``plans.delegation_notice`` to keep both files under the size
ratchet. The parent-notice table and its digest/wake decision still live
there; this module reaches back into it for the one shared summary shape
(``_STEP``, ``_summary``, ``_append``, ``_by_user``, ``_wake_parent``) rather than keeping a
second copy of any of them.
"""

from __future__ import annotations


def observe(
    connection,
    *,
    node_id: str,
    plan_id: str,
    title: str,
    event_id: str,
    actor_id: str | None,
    parent_plan_id: str,
    worker: str,
    created_at: str,
) -> None:
    """Digest one more attempt a rejection forced, and wake past the threshold.

    Ordinal counting matches ``operator_node_facts.revisions``, the one
    friction definition: the original submission is attempt 1, so a fresh
    revision's ordinal is one past however many rejections already forced the
    chain to reach it (never fewer than 2, since this only runs when a
    rejection just created this node). Every ordinal gets a digest; reaching
    ``commitment_cost.FRICTION_WAKE_ATTEMPTS`` also wakes the parent, once.
    """
    from plans import delegation_notice as notice
    from plans.commitment_cost import FRICTION_WAKE_ATTEMPTS
    from plans.operator_node_facts import revision_root, revisions

    ordinal = revisions([plan_id], db=connection).get(node_id, 0) + 1
    notice._append(
        connection,
        plan_id=parent_plan_id,
        event_id=event_id,
        actor_id=actor_id or worker,
        change_kind="plan.child_node_friction",
        summary=notice._summary(
            f"{notice._STEP} attempt {ordinal}", title, {},
            by_user=notice._by_user(actor_id),
        ),
        details={
            "node_id": node_id,
            "child_plan_id": plan_id,
            "worker_thread_id": worker,
            "attempt": ordinal,
        },
        created_at=created_at,
    )
    if ordinal >= FRICTION_WAKE_ATTEMPTS:
        # Identity is the step's own revision-chain root plus the threshold,
        # not the ordinal that crossed it: every later ordinal, a replay or a
        # restart names the message already accepted. The root, not
        # ``commitment_id``, because a worker's separate top-level steps all
        # share one commitment with the step the parent delegated.
        root_id = revision_root(node_id, db=connection)
        root = connection.execute(
            "SELECT title FROM plan_nodes WHERE id=?", (root_id,),
        ).fetchone()
        notice._wake_parent(
            connection, parent_plan_id, worker,
            kind="child_friction_threshold", subject_id=root_id,
            revision=FRICTION_WAKE_ATTEMPTS, actor_id=actor_id,
            title=str(root["title"]), attempts=ordinal,
        )
