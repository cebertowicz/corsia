"""Compose the bounded parent wake for a returned delegated worker."""

from __future__ import annotations


def notify(
    db, *, node_id: str, child_plan_id: str, worker_thread_id: str, reply: str = "",
) -> str | None:
    """Tell the parent its delegated work has returned and name the result to judge.

    One wake per attachment epoch: the harvest that calls this fires once per
    epoch. It carries the worker's steps, each one's state, the evidence it was
    put up for review with and the result artifacts that evidence names, so no
    second harvest is needed to learn the outcome. Bulky evidence stays where
    it is; the parent retrieves it when verification needs it. The wake is
    bounded like every default read: at most ``RETURNED_STEPS_MAX`` steps in
    plan order, titles and evidence cut to their byte caps, and a truthful
    ``omitted_count`` for the rest -- the child's plan is one read away.

    Returns the accepted input's id, or None when the parent could not take
    it (no model, or nothing left to act on). The harvest is one unit with
    this wake: a return the parent cannot take is not harvested yet, and
    waits, whole, for a later sweep.
    """
    from plans import delegation_harvest, delegation_reply, plan_wake, review_evidence, service
    from plans.close_next_bounds import bounded
    from prompts import plan_wakes as wakes

    node = plan_wake._node(db, node_id)
    plan = service._plan_row(db, node["plan_id"])
    if plan["owner_kind"] != "thread":
        return None
    leaves = delegation_harvest.return_rows(
        db, child_plan_id, delegation_harvest.attached_since(db, node_id),
    )
    steps = [delegation_reply.reply_step(reply)] if reply and not leaves else []
    for leaf in leaves[:plan_wake.RETURNED_STEPS_MAX]:
        evidence = review_evidence.latest(
            leaf["id"], requirements_revision=leaf["requirements_revision"], db=db,
        )
        said = _close_reason(db, leaf) if evidence is None else evidence["evidence"]
        steps.append({
            "id": leaf["id"],
            "title": bounded(str(leaf["title"]), plan_wake.RETURNED_TITLE_MAX_BYTES),
            "state": leaf["state"],
            "evidence": None if said is None else bounded(
                str(said), plan_wake.RETURNED_EVIDENCE_MAX_BYTES,
            ),
            "artifact_ids": [] if evidence is None else list(evidence["artifact_ids"]),
        })
    owner = str(plan["owner_id"])
    return plan_wake.append(
        db,
        recipient=owner,
        kind=plan_wake.CHILD_RETURNED,
        subject_id=node_id,
        revision=int(node["revision"]),
        plan_id=str(plan["id"]),
        content=plan_wake.with_next(
            db, str(plan["id"]), owner,
            wakes.WAKE_CHILD_RETURNED.format(title=str(node["title"])),
            returned={
                "child_plan_id": child_plan_id,
                "worker_thread_id": worker_thread_id,
                "steps": steps,
                "omitted_count": max(0, len(leaves) - plan_wake.RETURNED_STEPS_MAX),
                "total_count": len(leaves) or len(steps),
            },
        ),
        delegated_step_id=node_id,
        delegated_worker=worker_thread_id,
    )


def _close_reason(db, leaf) -> str | None:
    """A closed step's whole content is why it closed; that is its evidence."""
    from plans import closure

    return closure.close_reasons(db, [leaf]).get(str(leaf["id"]))
