"""A review's own verdict settles its review step.

A review step (``by: reviewer``) is settled by the verdict of the review it
was handed to: a pass accepts it, a fail sends it back to its author as one
unattached revision and keeps the run open. The author weighs the findings and
restarts the review by handing the revision back to the reviewer
(``delegate_send`` with ``plan_node_id``, the existing reuse path), so no
reviewer sees the work again before the author has.

The verdict is the reviewer's ``request_review`` ``verdict``, read when its
return is harvested (``settle_at_harvest``), or, for a step with a settle
script, only the script's report (``plans.review_settle_script``). Either way
the settle is a system write (no actor): the agent refusals on a reviewer step
are not for it, and what follows an accept or a send-back -- run end,
dependents, roll-ups, wakes -- is the same as for anyone's.
"""

from __future__ import annotations

import json
import logging

import plans.service as service

log = logging.getLogger(__name__)

PROVENANCE = "plans.review_settle"
VERDICTS = frozenset({"pass", "fail"})
#: The close reason of a review step its failed review sent back.
REVIEW_FAILED = "Sent back: review failed"
#: Refusal R1: the reviewer of a review step submits without a verdict.
VERDICT_REQUIRED = (
    "This step is a review you were handed: request_review needs verdict 'pass' or 'fail', "
    "with your findings in evidence. The verdict settles the review; you change nothing yourself."
)
#: Refusal R2: a verdict anywhere else.
VERDICT_ELSEWHERE = "verdict is only for the reviewer of a review step; submit this step with evidence alone."
#: What ``settle_at_harvest`` returns for a return that is no verdict to settle.
NOT_SETTLED = "not_settled"


def _reviewer_step(db, parent_plan_id: str, worker: str):
    """The open step in the parent plan this worker was handed, with its creation payload.

    The step is ``projections.delegated_step``'s (unarchived, unsettled, attached
    to exactly this thread), read with its creation event in one indexed read.
    """
    from plans.node_states import in_work_sql

    return db.execute(
        f"""SELECT n.id,(SELECT e.payload_json FROM plan_node_events e
                        WHERE e.node_id=n.id AND e.event='node_created'
                        ORDER BY e.created_at,e.id LIMIT 1) AS created_json
           FROM plan_nodes n
           WHERE n.plan_id=? AND n.spawned_thread_id=? AND {in_work_sql('n')}
             AND n.state IN ('pending','active','review_requested')
           ORDER BY n.id LIMIT 1""",
        (parent_plan_id, worker),
    ).fetchone()


def _settled_by_review(created: dict) -> bool:
    """Whether a step with this creation payload is settled by its reviewer's own verdict."""
    return created.get("workflow_by") == "reviewer" and not created.get("workflow_settle")


def check_verdict(db, node_id: str, thread_id: str | None, verdict) -> None:
    """Require a verdict from the reviewer of a review step and refuse one anywhere else.

    The reviewer is the thread whose own plan holds ``node_id`` and that is the
    worker on a review step of its parent plan with no settle script.
    """
    if verdict is not None and verdict not in VERDICTS:
        raise service.PlanInputError("verdict must be 'pass' or 'fail'")
    reviewer = False
    if thread_id is not None:
        plan = service._plan_row(db, service._node_row(db, node_id)["plan_id"])
        if plan["owner_kind"] == "thread" and plan["owner_id"] == thread_id and plan["parent_plan_id"]:
            step = _reviewer_step(db, plan["parent_plan_id"], thread_id)
            reviewer = step is not None and _settled_by_review(json.loads(step["created_json"] or "{}"))
    if reviewer and verdict is None:
        raise service.PlanError(VERDICT_REQUIRED)
    if not reviewer and verdict is not None:
        raise service.PlanError(VERDICT_ELSEWHERE)


def _returned_verdict(db, node) -> tuple[str | None, str]:
    """The verdict and findings of the latest evidence on the steps this return handed back."""
    from plans import delegation_harvest

    ids = [str(leaf["id"]) for leaf in delegation_harvest.returned_steps(db, node)]
    if not ids:
        return None, ""
    row = db.execute(
        f"""SELECT payload_json FROM plan_node_events
            WHERE node_id IN ({",".join("?" * len(ids))}) AND event='review_evidence_submitted'
            ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        ids,
    ).fetchone()
    payload = json.loads(row["payload_json"] or "{}") if row is not None else {}
    verdict = payload.get("verdict")
    return (verdict if verdict in VERDICTS else None), str(payload.get("evidence") or "")


def settle_at_harvest(db, node, worker: str):
    """Settle a harvested review step from its reviewer's verdict, inside the harvest's write.

    Returns ``NOT_SETTLED`` when this return is no verdict to settle: not a
    review step, one a settle script settles, a return with no verdict (a
    reviewer started before verdicts existed), or a reviewer that has seen the
    work (``fresh_reviewer``), whose return the author judges as before.
    Otherwise the author's wake id, or None when the author can't take it.
    """
    from plans import fresh_reviewer
    from plans.workflow_fields import creation_payload

    node = service._node_row(db, node["id"])
    if not _settled_by_review(creation_payload(db, node["id"])):
        return NOT_SETTLED
    verdict, findings = _returned_verdict(db, node)
    if verdict is None or fresh_reviewer.has_seen_the_work(db, node, worker):
        return NOT_SETTLED
    # A verdict the plan can't take now (a hold or an unsettled dependency on
    # the step) leaves the return to its author, as before verdicts existed.
    db.execute("SAVEPOINT review_settle")
    try:
        _outcome, wake = settle(
            db, str(node["id"]), verdict=verdict, findings=findings, verified_by=worker, reviewer=worker,
        )
    except service.PlanError:
        log.warning("review settle of %s refused; its author judges the return", node["id"], exc_info=True)
        db.execute("ROLLBACK TO review_settle")
        db.execute("RELEASE review_settle")
        return NOT_SETTLED
    db.execute("RELEASE review_settle")
    return wake


def into_review(db, node_id: str, evidence: str) -> None:
    """Put a started review step up for review on this evidence; one already there stays."""
    from plans import review_evidence

    if service._node_row(db, node_id)["state"] == "active":
        review_evidence.submit(
            node_id, evidence=evidence, expected_revision=None, actor_id=None,
            provenance=PROVENANCE, db=db,
        )


def settle(
    db, node_id: str, *, verdict: str, findings: str, verified_by: str, reviewer: str | None,
) -> tuple[str, str | None]:
    """Settle one review step from its verdict; the outcome and the author's wake id.

    ``accepted``: the step is accepted as the boundary's accept does it, the
    findings its evidence. ``sent_back``: the step closes as sent back with the
    findings and one unattached revision opens beside it (``send_back``).
    """
    from plans import archival, boundary_after, review_evidence

    findings = service._required_text(findings, "review findings")
    node = service._node_row(db, node_id)
    author = _author(db, node)
    if verdict != "pass":
        revision = send_back(db, node_id, findings=findings, reason=REVIEW_FAILED, verified_by=verified_by)
        return "sent_back", None if author is None else _wake_failed(db, node_id, revision, author, reviewer, findings)
    if node["state"] == "active":
        into_review(db, node_id, findings)
    else:
        review_evidence.record_verdict(
            node_id, findings, verdict="Accepted", actor_id=None, provenance=PROVENANCE, db=db,
        )
    archival.settle_watches(db, node_id)
    service.transition_node(
        node_id, "accepted", verification_result="passed", verified_by=verified_by,
        actor_id=None, provenance=PROVENANCE, db=db,
    )
    common = {"actor_id": None, "provenance": PROVENANCE, "db": db}
    # The author is told by the one wake below, which carries its next block.
    boundary_after.after_close(node_id, "accepted", {}, common, executor=author)
    return "accepted", None if author is None else _wake_passed(db, node_id, author, reviewer, findings)


def send_back(
    db, node_id: str, *, findings: str, reason: str, verified_by: str,
    correction: bool = True, tell_author: bool = False,
):
    """Send a review step back to its author: closed, with one unattached revision beside it.

    The reviewer's returned steps are accepted (its review was done); the
    reviewer is neither stood down nor handed the revision. No node fails, so
    the run stays open. ``tell_author`` lets reconciliation wake the author as
    for any write; otherwise the caller's own wake tells it.
    """
    from plans import admission, rejection

    into_review(db, node_id, findings)
    node = service._node_row(db, node_id)
    _rejected, revision, replayed = rejection.reject_submission(
        node_id, feedback=findings, actor_id=None, provenance=PROVENANCE, db=db,
        review_done_by=verified_by, reason=reason, correction=correction,
    )
    if not replayed:
        admission.release(node_id, "review_rejected", db=db)
        admission.reconcile_plan(
            node["plan_id"], actor_id=None, provenance=PROVENANCE,
            causing_executor=None if tell_author else _author(db, node), db=db,
        )
    return revision


def _author(db, node) -> str | None:
    plan = service._plan_row(db, node["plan_id"])
    return str(plan["owner_id"]) if plan["owner_kind"] == "thread" else None


def _wake_passed(db, node_id: str, author: str, reviewer: str | None, findings: str) -> str | None:
    from plans import plan_wake
    from plans.close_next_bounds import bounded
    from prompts import plan_wakes as wakes

    node = service._node_row(db, node_id)
    return plan_wake.append(
        db, recipient=author, kind=plan_wake.REVIEW_PASSED, subject_id=node_id,
        revision=int(node["revision"]), plan_id=str(node["plan_id"]),
        content=plan_wake.with_next(
            db, str(node["plan_id"]), author, wakes.WAKE_REVIEW_PASSED.format(title=str(node["title"])),
            review={
                "reviewer": reviewer,
                "evidence": bounded(findings, plan_wake.RETURNED_EVIDENCE_MAX_BYTES),
            },
        ),
    )


def _wake_failed(db, node_id: str, revision, author: str, reviewer: str | None, findings: str) -> str | None:
    from plans import handoff_brief, plan_wake
    from plans.close_next_bounds import REVISION_BRIEF_MAX_BYTES
    from prompts import plan_wakes as wakes

    node = service._node_row(db, node_id)
    plan_id = str(node["plan_id"])
    field = handoff_brief.bounded_field(db, node, plan_id)
    return plan_wake.append(
        db, recipient=author, kind=plan_wake.REVIEW_FAILED, subject_id=node_id,
        revision=int(node["revision"]), plan_id=plan_id,
        content=plan_wake.with_next(
            db, plan_id, author,
            wakes.WAKE_REVIEW_FAILED.format(title=str(node["title"]), revision_id=revision.id, reviewer=reviewer),
            review={
                "reviewer": reviewer, "revision_id": revision.id,
                "findings": field(findings, REVISION_BRIEF_MAX_BYTES, what="feedback"),
            },
        ),
        open_node_id=revision.id,
    )
