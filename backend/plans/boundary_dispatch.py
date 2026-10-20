"""Operation routing for the actor-bound plan mutation boundary."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import plans.admission as admission
import plans.boundary_after as after
import plans.boundary_edges as boundary_edges
import plans.boundary_fields as fields
import plans.lifecycle as lifecycle
import plans.user_settle as user_settle
import plans.user_step_settle as user_step_settle
import plans.mutations as mutations
import plans.pause_authority as pause_authority
import plans.rejection as rejection
import plans.service as service
import plans.triage_lines as triage_lines
import plans.user_steering as user_steering
import plans.watch_operations as watch_operations
from prompts import plan_tools as text, plan_wakes as wakes

#: The refusal for finishing active work with nothing to rest the verdict on.
FINISHED_WORK_NEEDS_EVIDENCE = (
    "finished active work needs evidence: use mark_done with evidence. "
    "close_node is for work you are not doing."
)
MARK_DONE_REVIEW = "mark_done is for active self-completion; use accept for work awaiting review"


def _dispatch_hold(operation: str, params: dict[str, Any], actor, common: dict):
    """Route clearing a task's pause or blocker cause through the one hold owner.

    ``actor`` is carried because clearing a cause reselects the task: an
    executor resuming its own held work must not be woken about its own write.
    """
    from plans import holds

    acting = actor.thread_id if actor.kind == "thread" else None
    kind = "paused" if operation == "resume_node" else "blocked"
    if acting is not None and kind == "paused":
        user_steering.refuse_agent_resume(
            common["db"], params["node_id"], fields.text(params, "hold_id"),
        )
    return holds.clear(
        params["node_id"], fields.text(params, "hold_id"),
        expected_kind=kind, causing_executor=acting,
        **fields.expected(params), **common,
    )


#: What an agent's activate or start answers for a task assigned to the user.
USER_ASSIGNED_CONFLICT = {"condition": "user_assigned"}


def _dispatch_admission(operation: str, params: dict[str, Any], actor, common: dict):
    """Route executor selection mutations to the canonical admission owner.

    ``activate`` and ``start`` are interchangeable here: admission picks the
    move the node's own state needs. A task assigned to the user is the user's
    to complete: an agent's activate or start leaves it untouched and reports
    ``USER_ASSIGNED_CONFLICT``. When another task holds the slot the reply
    carries a notice saying the task did not start and how to start it.
    """

    node = service._node_row(common["db"], params["node_id"])
    if actor.kind == "thread" and node["assignee"] == "user":
        conflict = dict(USER_ASSIGNED_CONFLICT)
        return admission.result(node["id"], db=common["db"], conflict=conflict)
    jump = triage_lines.jump_for_start(
        common["db"], node, actor_id=actor.thread_id if actor.kind == "thread" else None,
    )
    reply = admission.make_current(
        params["node_id"], **fields.expected(params), **common,
        payload=None if jump is None else jump.payload,
    )
    if jump is not None and reply["admission"].get("selection") is not None:
        reply["jumped"] = jump.line
    conflict = reply["admission"].get("conflict") or {}
    holder = conflict.get("selected_node_id")
    if holder and holder != node["id"]:
        reply["notice"] = text.start_slot_held(
            conflict.get("selected_node_title") or holder, holder, node["id"],
        )
    return reply


#: Operations that change the plan's shape or a node's requirements.
_STRUCTURE_OPERATIONS = frozenset(
    {"add", "insert", "edit", "split", "reorder", "add_correction"}
)
#: Operations that move a node toward or into a settled state.
_SETTLEMENT_OPERATIONS = frozenset(
    {
        "request_review", "mark_done", "reject", "fail", "attach_spawned_thread",
        "replace_spawned_thread", "accept", "accept_with_follow_up", "close_node",
    }
)


def _reorder(params: dict[str, Any], common: dict) -> dict:
    """Move one node; its siblings keep their revisions (service.touch_node)."""
    from plans.boundary import node_projection

    moved = mutations.reorder_node(
        params["node_id"], fields.integer(params, "order_key"),
        **fields.expected(params), **common,
    )
    return {"node": node_projection(moved)}


def _dispatch_structure(plan_id, operation, params, common, db, workflow_version, actor):
    """Add, reshape, or re-specify nodes through the canonical evidence paths."""
    acting = actor.thread_id if actor.kind == "thread" else None
    if operation in {"add", "insert"}:
        from plans import intent_creation

        created, resolved_words = intent_creation.prepare(
            db, plan_id, params, workflow_version=workflow_version, operation=operation, actor=actor,
        )
        node = service.add_node(plan_id, **created, **common) if operation == "add" else mutations.insert_node(plan_id, **created, **common)
        intent_creation.capture(db, plan_id, node, resolved_words, actor=actor)
        if "depends_on_node_id" in params:
            from plans import dependencies

            dependencies.add_dependency(
                node.id, fields.text(params, "depends_on_node_id"),
                causing_executor=acting, **common,
            )
        line = (
            triage_lines.add_line(db, service._plan_row(db, plan_id), node.id, actor_id=acting)
            if operation == "add" else None
        )
        if line is None:
            return node
        from plans.boundary import node_projection
        return {"node": node_projection(node), "rank": line}
    if operation == "edit":
        from plans import intent_words
        changed = fields.edit_fields(params)
        if "intent_words" in changed:
            changed["intent_words"] = intent_words.resolve_words(
                db, plan_id, changed["intent_words"], actor_id=acting,
            )
        mutations.edit_node(
            params["node_id"], **changed, **fields.expected(params), **common,
        )

        return admission.result(params["node_id"], db=db)
    if operation == "split":
        from plans import intent_words

        requested_words = params.get("intent_words")
        return mutations.split_node(
            params["node_id"], title=fields.text(params, "title"),
            acceptance_criteria=fields.text(params, "acceptance_criteria"),
            description=params.get("description"), size=fields.require_task_size(params.get("size")),
            intent=params.get("intent"),
            intent_words=intent_words.resolve_words(db, plan_id, requested_words, actor_id=acting),
            **fields.expected(params), **common,
        )
    if operation == "reorder":
        return _reorder(params, common)
    from plans import corrections

    return corrections.add(
        params["node_id"],
        title=fields.text(params, "title"),
        acceptance_criteria=fields.text(params, "acceptance_criteria"),
        affected_nodes=params.get("affected_nodes"),
        **fields.expected(params),
        **common,
    )


def _replace_worker(node_id: str, params: dict, common: dict) -> dict:
    """Replace a step's worker; name the old worker's own workers left running."""

    result = mutations.replace_spawned_thread(
        node_id, fields.text(params, "spawned_thread_id"),
        **fields.expected(params), **common,
    )
    reply: dict = {"node": asdict(result.node)}
    if result.replaced is not None:
        reply["replaced_worker"] = result.replaced
        notice = wakes.REPLACE_OLD_WORKER.format(thread=result.replaced)
        if result.stranded:
            reply["stranded_workers"] = result.stranded
            notice += wakes.REPLACE_STRANDED.format(threads=", ".join(result.stranded))
        reply["notice"] = notice
    return reply


def _mark_done(params: dict[str, Any], actor, common: dict, db) -> dict:
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
    expected = _submit_finished_work(node_id, params, common, actor.thread_id, db)
    service.transition_node(
        node_id, "accepted", verification_result="passed",
        verified_by=actor.audit_id, **expected, **common,
    )
    return after.after_close(
        node_id, "accepted", params, common, executor=actor.thread_id,
    )


def _dispatch_settlement(operation: str, params: dict[str, Any], actor, common, db):
    """Submit for review, reject, fail, attach a worker, or accept.

    Acceptance first attributes a still-unbound reviewer Turn to the accepted
    node's commitment, so verification cost lands on the work it judged rather
    than on the reviewer's own plan. That attribution never refuses acceptance.
    """
    node_id = params["node_id"]
    # Who is acting. A human actor executes nothing, so every executor this
    # settlement touches learns about it.
    acting = actor.thread_id if actor.kind == "thread" else None
    if operation == "mark_done":
        return _mark_done(params, actor, common, db)
    if operation == "request_review":
        mutations.request_review(
            node_id,
            evidence=fields.text(params, "evidence"),
            artifact_ids=params.get("artifact_ids"),
            owner_thread_id=acting,
            **fields.expected(params),
            **common,
        )
        return after.after_close(
            node_id, "review_requested", params, common, executor=acting,
        )
    if operation == "reject":
        return rejection.dispatch_reject(node_id, params, common, executor=acting)
    if operation == "fail":
        _record_verdict(params, "Failed", common)
        mutations.fail_node(node_id, **fields.expected(params), **common)
        return after.after_close(node_id, "failed", params, common, executor=acting)
    if operation == "close_node":
        import plans.closure as closure

        closure.close_node(
            node_id, reason=fields.text(params, "reason"),
            **fields.expected(params), **common,
        )
        return after.after_close(
            node_id, service.CLOSED, params, common, executor=acting,
        )
    if operation == "attach_spawned_thread":
        return mutations.attach_spawned_thread(
            node_id, fields.text(params, "spawned_thread_id"),
            **fields.expected(params), **common,
        )
    if operation == "replace_spawned_thread":
        return _replace_worker(node_id, params, common)

    fields.require_passed_verification(params)
    from plans import archival, review_usage

    archival.settle_watches(db, node_id)

    review_usage.bind(
        node_id,
        reviewer_thread_id=actor.thread_id,
        turn_id=actor.turn_id,
        db=db,
    )
    expected = _submit_finished_work(node_id, params, common, acting, db)
    if operation == "accept":
        service.transition_node(
            node_id, "accepted", verification_result="passed",
            verified_by=actor.audit_id, **expected, **common,
        )
        return after.after_close(node_id, "accepted", params, common, executor=acting)
    from plans import intent_words

    _node, follow_up = service.accept_with_follow_up(
        node_id, title=fields.text(params, "title"),
        acceptance_criteria=fields.text(params, "acceptance_criteria"),
        size=fields.require_task_size(params.get("size")), intent=params.get("intent"),
        intent_words=intent_words.resolve_words(db, service._node_row(db, node_id)["plan_id"], params.get("intent_words"), actor_id=acting),
        verification_result="passed", verified_by=actor.audit_id,
        **expected, **common,
    )
    result = after.after_close(node_id, "accepted", params, common, executor=acting)
    result["follow_up"] = asdict(follow_up)
    return result


def _submit_finished_work(
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
        _record_verdict(params, "Accepted", common)
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


def _record_verdict(params: dict[str, Any], verdict: str, common: dict) -> None:
    """Record the settler's evidence first, so an accept can rest on it (#1977)."""
    from plans import review_evidence

    if "evidence" not in params:
        return
    review_evidence.record_verdict(
        params["node_id"], fields.text(params, "evidence"), verdict=verdict,
        **common,
    )


def dispatch(plan_id, operation, params, actor, provenance, db, *, workflow_version=None):
    """Route one validated mutation without creating a second plan service.

    The user's Mark done and Won't do settle through ``user_settle``. Their
    pause or resume of a task takes the same path an agent's does;
    ``user_steering`` only fills and checks it first, moves the worker's plan
    instead for a delegated task, and tells the owner after.
    """
    common = {"actor_id": actor.audit_id, "provenance": provenance, "db": db}
    if actor.kind != "user" or operation not in user_steering.STEERING_OPERATIONS:
        return _route(plan_id, operation, params, actor, common, db, workflow_version)
    if operation in user_settle.OUTCOMES:
        return user_settle.settle(operation, params, actor.audit_id, common)
    params = user_steering.prepare(operation, params, db)
    if "worker_plan_id" in params:
        return user_steering.move_worker_plan(operation, params, common)
    result = _route(plan_id, operation, params, actor, common, db, workflow_version)
    user_steering.tell_owner(operation, params, common)
    return result


def _route(plan_id, operation, params, actor, common, db, workflow_version):
    """Send one mutation to the owner of its operation."""
    if operation in boundary_edges.EDGE_OPERATIONS:
        return boundary_edges.dispatch(plan_id, operation, params, actor, common, db)
    if operation in {"pause", "resume", "pause_node"}:
        pause_authority.require(operation, params, actor)
    if operation in {"pause", "resume"}:
        return lifecycle.transition(
            plan_id,
            "paused" if operation == "pause" else "active",
            expected_revision=params["expected_revision"],
            causing_executor=actor.thread_id if actor.kind == "thread" else None,
            **common,
        )
    if operation in fields.PARK_OPERATIONS:
        return after.after_park(plan_id, operation, params, actor, common)
    if operation in {"resume_node", "resolve_blocker"}:
        return _dispatch_hold(operation, params, actor, common)
    if operation in {"activate", "start"}:
        return _dispatch_admission(operation, params, actor, common)
    if operation == "resolve_watch":
        return watch_operations.resolve(plan_id, params, actor, db)
    if operation in _STRUCTURE_OPERATIONS:
        return _dispatch_structure(
            plan_id, operation, params, common, db, workflow_version, actor,
        )
    if operation in _SETTLEMENT_OPERATIONS:
        return _dispatch_settlement(operation, params, actor, common, db)
    raise service.PlanInputError(f"unsupported plan operation: {operation}")
