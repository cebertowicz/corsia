"""Authorization and precondition checks for structured plan decisions.

Separated from ``plans.decisions`` the same way ``plans.boundary`` is separated
from ``plans.service``: this module decides *who may act and when*, and never
writes. Every refusal uses one fixed message so a decision, node, or plan id can
never be probed for existence through this surface.
"""

from __future__ import annotations

import plans.boundary as boundary
import plans.decision_projection as decision_projection
import plans.errors as errors
import plans.intake as intake
import plans.node_states as node_states
import plans.service as service

UNAVAILABLE = "plan decision is unavailable"
ACTOR_DENIED = "this actor may not perform that decision operation"


def source_node(
    db, plan_id: str, node_id, *, settled_ok: bool = False, explain: bool = False,
):
    """Resolve one source node inside an already authorized plan.

    Only open work takes a new or changed question. ``settled_ok`` admits a
    closed or archived source for withdrawal alone, so a question stranded
    before closing withdrew it can still be cleared (#2651). ``explain`` names
    that cause to the agent that asked instead of the fixed refusal; the plan is
    already authorized, so naming it discloses nothing. The human answer route
    keeps the fixed refusal its client reads as unavailable.
    """
    if not isinstance(node_id, str) or not node_id.strip():
        raise service.PlanError("source_node_id is required")
    row = db.execute(
        "SELECT *,"
        f" ({node_states.in_work_sql('n')}"
        f" AND {node_states.outside_retired_branch_sql('n')}) AS open_work"
        " FROM plan_nodes n WHERE n.id=? AND n.plan_id=?",
        (node_id.strip(), plan_id),
    ).fetchone()
    if row is None:
        raise PermissionError(UNAVAILABLE)
    if not row["open_work"] and not settled_ok:
        if not explain:
            raise PermissionError(UNAVAILABLE)
        raise service.PlanError(
            f"source task {row['id']} is no longer open work:"
            " a decision on it can only be withdrawn (invalidate)"
        )
    return row


def tiers(db, plan) -> tuple[str, str | None]:
    """Resolve (author, reviewer) exactly as the plan review model does.

    Only a plan's owner runs its tasks, so the plan owner authors and the
    thread that spawned it reviews. Root work has no parent tier, so the
    reviewer is ``None``.
    """
    author = plan["owner_id"]
    row = db.execute(
        "SELECT spawned_by FROM threads WHERE id=?", (author,),
    ).fetchone()
    return author, (row["spawned_by"] if row is not None else None)


def require_tier(actor, allowed) -> None:
    """Require a current thread actor holding one of the permitted tiers."""
    permitted = {tier for tier in allowed if tier}
    if actor.kind != "thread" or not actor.thread_id:
        raise PermissionError(ACTOR_DENIED)
    if actor.thread_id not in permitted:
        raise PermissionError(ACTOR_DENIED)


def require_author(db, plan, source, actor) -> None:
    """Only the thread that owns the work may state its decision."""
    author, _reviewer = tiers(db, plan)
    require_tier(actor, (author,))


def require_reviewer(db, plan, source, actor) -> None:
    """Only the parent tier reviews or raises; root work may self-review.

    Root work genuinely has no higher tier inside the plan, which is the same
    fallback ``boundary._authorize_reviewer`` already grants there. A delegated
    child can never approve its own decision.
    """
    author, reviewer = tiers(db, plan)
    require_tier(actor, (reviewer,) if reviewer else (author,))


def require_author_or_reviewer(db, plan, source, actor) -> None:
    """Either tier may change, reorder, or withdraw a queued decision (E8)."""
    author, reviewer = tiers(db, plan)
    require_tier(actor, (author, reviewer))


def require_human(actor) -> None:
    """Only an authenticated human answers a decision."""
    if actor.kind != "user" or not actor.user_id:
        raise PermissionError(ACTOR_DENIED)


def require_revision(decision: dict, expected) -> None:
    """Reject a change computed against a superseded decision revision.

    The refusal states the current revision so the retry needs no re-read, and
    still names no identity: the caller reached it only after authorization.
    """
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise service.PlanError("expected_revision is required")
    if decision["revision"] != expected:
        raise errors.revision_conflict(
            "plan decision", None, expected, decision["revision"],
        )


def require_state(decision: dict, allowed) -> None:
    """Enforce the decision state machine without disclosing any identity."""
    if decision["state"] not in allowed:
        raise service.PlanError(
            f"a {decision['state']} decision cannot accept that change"
        )


class DecisionClosed(service.PlanError):
    """The question left the queue before this answer arrived.

    Neither retryable nor fixable in place, so it is neither a 409 (re-read and
    resend) nor a 422 (edit and resend). The message is written for the person
    who sent the answer; it names no identity and discloses nothing the
    authorized caller could not already read.
    """

    status = 410


_CLOSED_MESSAGES = {
    "answered": "This was already answered.",
    "invalidated": "This question was withdrawn.",
}


def require_open_for_answer(decision: dict) -> None:
    """Refuse an answer to a decision that is already settled or withdrawn.

    Runs before the revision check: settling or withdrawing a decision moves its
    revision, so a stale client would otherwise be told "changed, reloading" for
    a question that will never come back. Earlier lifecycle states keep the
    ordinary state refusal.
    """
    message = _CLOSED_MESSAGES.get(decision["state"])
    if message is not None:
        raise DecisionClosed(message)
    require_state(decision, ("raised",))


def resolve(db, decision_id, actor, *, settled_source_ok: bool = False):
    """Authorize the owning plan before any decision existence is disclosed."""
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise service.PlanError("decision_id is required")
    row = decision_projection.identity_row(db, decision_id.strip())
    body = decision_projection._body(row) if row is not None else None
    if body is None:
        raise PermissionError(UNAVAILABLE)
    plan, bound = intake._authorize_thread_plan(db, body["plan_id"], actor)
    source = source_node(
        db, plan["id"], body["source_node_id"], settled_ok=settled_source_ok,
        explain=bound.kind == "thread",
    )
    return plan, decision_projection.project(row, body), source, bound


def authorized_plan(db, plan_id: str, actor):
    """Resolve one accessible live thread plan for a fresh decision draft."""
    return intake._authorize_thread_plan(db, plan_id, actor)


def required_mutation_id(value: str) -> str:
    """Require the caller's durable idempotency key, as every plan write does."""
    return boundary._required_mutation_id(value)
