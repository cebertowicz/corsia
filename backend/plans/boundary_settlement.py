"""Evidence-lifecycle helpers used by the plan mutation dispatcher.

``boundary_dispatch`` remains the sole operation router. These helpers keep
settlement's evidence, state transition, and notification paths together
without making that router the owner of every implementation detail.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import plans.admission as admission
import plans.boundary_after as after
import plans.boundary_fields as fields
import plans.mutations as mutations
import plans.service as service
import plans.user_step_settle as user_step_settle
from prompts import plan_tools as text

#: The refusal for finishing active work with nothing to rest the verdict on.
FINISHED_WORK_NEEDS_EVIDENCE = (
    "finished active work needs evidence: use mark_done with evidence. "
    "close_node is for work you are not doing."
)
MARK_DONE_REVIEW = "mark_done is for active self-completion; use accept for work awaiting review"


def mark_done(params: dict[str, Any], actor, common: dict, db) -> dict:
    """Accept the agent's own active work through the existing evidence lifecycle."""
    node_id = params["node_id"]
    node = service._node_row(db, node_id)
    state = node["state"]
    if node["assignee"] == "user":
        return user_step_settle.mark_done(params, actor, common, db)
    if state == "pending":
        holder = admission.selection_conflict(db, node)
        if holder is not None:
            raise service.PlanError(text.mark_done_slot_held(
                holder["selected_node_title"], holder["selected_node_id"], node_id,
            ))
        admission.make_current(node_id, **fields.expected(params), **common)
        node = service._node_row(db, node_id)
        state = node["state"]
        params = {**params, "expected_revision": node["revision"]}
    if state == "review_requested":
        raise service.PlanError(MARK_DONE_REVIEW)
    if state != "active":
        raise service.PlanError(
            f"mark_done settles pending or active work; this task is {state}"
        )
    fields.require_passed_verification(params)
    from plans import archival

    archival.settle_watches(db, node_id)
    expected = submit_finished_work(node_id, params, common, actor.thread_id, db)
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by=actor.audit_id, **expected, **common,
    )
    return after.after_close(node_id, "accepted", params, common, executor=actor.thread_id)


def accept(
    operation: str, node_id: str, params: dict[str, Any], actor, common: dict, db, acting: str | None,
) -> dict:
    """Accept reviewed work, including reviewer-cost attribution and follow-ups."""
    fields.require_passed_verification(params)
    from plans import archival, intent_words, review_usage

    archival.settle_watches(db, node_id)
    review_usage.bind(
        node_id, reviewer_thread_id=actor.thread_id, turn_id=actor.turn_id, db=db,
    )
    expected = submit_finished_work(node_id, params, common, acting, db)
    if operation == "accept":
        service.transition_node(
            node_id, "accepted", verification_result="passed",
            verified_by=actor.audit_id, **expected, **common,
        )
        return after.after_close(node_id, "accepted", params, common, executor=acting)
    _node, follow_up = service.accept_with_follow_up(
        node_id, title=fields.text(params, "title"),
        acceptance_criteria=fields.text(params, "acceptance_criteria"),
        size=fields.require_task_size(params.get("size")), intent=params.get("intent"),
        intent_words=intent_words.resolve_words(db, params.get("intent_words"), actor_id=acting),
        verification_result="passed", verified_by=actor.audit_id,
        **expected, **common,
    )
    result = after.after_close(node_id, "accepted", params, common, executor=acting)
    result["follow_up"] = asdict(follow_up)
    return result


def fail(node_id: str, params: dict, common: dict, db, acting: str | None) -> dict:
    """Record a failed verdict, transition it, and notify the prior executor."""
    record_verdict(params, "Failed", common)
    mutations.fail_node(node_id, **fields.expected(params), **common)
    from core.self_caught_corrections import record_failed_plan_node

    record_failed_plan_node(db, node_id)
    return after.after_close(node_id, "failed", params, common, executor=acting)


def submit_finished_work(
    node_id: str, params: dict[str, Any], common: dict, acting: str | None, db,
) -> dict[str, Any]:
    """Put finished active work up for review on the settler's evidence.

    Accepting work you did yourself used to take request_review, then accept;
    close_node took one call, so finished work was closed and never counted as
    done. An accept on active work now submits its evidence first, through the
    same writer request_review uses, and returns the revision the accept then
    expects. Work already awaiting review records the evidence as the verdict.
    """
    if service._node_row(db, node_id)["state"] != "active":
        record_verdict(params, "Accepted", common)
        return fields.expected(params)
    if "evidence" not in params:
        raise service.PlanInputError(FINISHED_WORK_NEEDS_EVIDENCE)
    mutations.request_review(
        node_id, evidence=fields.text(params, "evidence"),
        artifact_ids=params.get("artifact_ids"),
        owner_thread_id=acting, expected_revision=params["expected_revision"],
        **common,
    )
    return {"expected_revision": service._node_row(db, node_id)["revision"]}


def record_verdict(params: dict[str, Any], verdict: str, common: dict) -> None:
    """Record the settler's evidence first, so an accept can rest on it (#1977)."""
    from plans import review_evidence

    if "evidence" not in params:
        return
    review_evidence.record_verdict(
        params["node_id"], fields.text(params, "evidence"), verdict=verdict,
        **common,
    )
