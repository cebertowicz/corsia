"""Actor-bound current-plan read, ensure, and mutation boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, cast

import infra.config as config
from infra.corsia_agent import get_corsia_agent
from infra.queries import user_in_project
import plans.blocked_wait as blocked_wait
import plans.boundary_actor as boundary_actor
import plans.boundary_dispatch as boundary_dispatch
import plans.boundary_fields as fields
import plans.boundary_guard as boundary_guard
import plans.boundary_worker as boundary_worker
from plans.conductor_add import conductor_may_add, thread_may_mutate
import plans.errors as errors
import plans.node_settlement as node_settlement
import plans.notifications as notifications
import plans.receipts as receipts
import plans.scheduled_wait as scheduled_wait
import plans.service as service


def is_user_audit_id(actor_id: str | None) -> bool:
    """Whether this audit identity (``PlanActor.audit_id``) is the user's, not a thread's."""
    return bool(actor_id) and str(actor_id).startswith("user:")


@dataclass(frozen=True)
class PlanActor:
    """A server-bound thread or explicitly human identity at this boundary."""

    thread_id: str | None
    user_id: str
    project_id: str | None
    kind: str = "thread"
    turn_id: str | None = None
    #: The ids the running tool call is known by: CORSIA's invocation id and the
    #: provider's own call id. An assistant step lists its calls by one of them,
    #: so a hold's origin can find the step that raised it.
    tool_invocation_id: str | None = None
    provider_call_id: str | None = None

    @property
    def audit_id(self) -> str:
        """Return the durable actor identity that audit events may record."""
        return cast(str, self.thread_id) if self.kind == "thread" else f"user:{self.user_id}"


def node_projection(node) -> dict[str, Any]:
    """Return the stable public representation for a mutated plan node."""
    return asdict(node)


def read_current_plan(
    *, actor: PlanActor, cursor: str | None = None,
    known_revision: int | None = None, include_folded: bool | None = None,
    thread_id: str | None = None, db,
):
    """Read one page of the actor's thread plan, or ``thread_id``'s, without creating it."""
    from plans.current import read_current_plan as read

    return read(
        actor=actor, cursor=cursor, known_revision=known_revision,
        include_folded=include_folded, thread_id=thread_id, db=db,
    )


def ensure_current_plan(
    *, actor: PlanActor, cursor: str | None = None,
    known_revision: int | None = None, include_folded: bool | None = None, db,
):
    """Ensure the current actor-authorized thread plan idempotently and read one page."""
    from plans.current import ensure_current_plan as ensure

    return ensure(
        actor=actor, cursor=cursor, known_revision=known_revision,
        include_folded=include_folded, db=db,
    )


def mutate(
    plan_id: str,
    operation: str,
    params: dict[str, Any],
    *,
    actor: PlanActor,
    provenance: str,
    mutation_id: str,
    db,
    workflow_version: str | None = None,
    workflow_steps: tuple = (),
) -> notifications.DeferredMutation:
    """Atomically authorize, execute, and receipt one idempotent mutation.

    ``workflow_version`` is the server-resolved version of the workflow skill
    an ``add`` or ``insert`` names, and ``workflow_steps`` the plain steps a
    phase running it is built from (``plans.workflow_fields.build_run``);
    neither is a caller parameter.
    """
    fields.validate_operation_params(operation, params)
    mutation_id = _required_mutation_id(mutation_id)
    changed = False
    drives: tuple[str, ...] = ()
    with notifications.collect_touched() as touched, service._write(db):
        actor = _revalidate_actor(db, actor)
        try:
            plan = _authorize_plan_scope(db, plan_id, actor)
        except errors.UnknownPlan:
            if operation == "add" and actor.kind == "thread":
                from prompts.plan_tools import FOREIGN_PLAN_MISSING

                raise errors.PlanRefusal(FOREIGN_PLAN_MISSING) from None
            raise
        message_floor = notifications.capture_message_floor(db)
        from registry.node_retirement import capture_event_floor

        artifact_event_floor = capture_event_floor(db)
        if plan["owner_kind"] != "thread":
            raise PermissionError("public scope plan mutation is unavailable")
        fingerprint = receipts.request_fingerprint(operation, params)
        replay = receipts.load(
            db, plan_id=plan_id, mutation_id=mutation_id, actor_id=actor.audit_id,
            operation=operation, fingerprint=fingerprint,
        )
        if replay is not None:
            result = replay
        else:
            result = _authorize_and_dispatch(
                db, plan_id, operation, params, actor, provenance, plan,
                workflow_version, workflow_steps,
            )
            receipts.record(
                db, plan_id=plan_id, mutation_id=mutation_id,
                actor_id=actor.audit_id, operation=operation,
                fingerprint=fingerprint, result=result,
            )
            changed = True
            drives = notifications.wake_recipients(message_floor, db=db)
    if changed:
        # A bulk archive releases and reconciles inside its own dispatch, but
        # names no next task, so it has no close-next fact to record.
        if operation in fields.NEXT_OPERATIONS and "next" in result:
            from plans import close_next_event

            close_next_event.emit_close_event(operation, result, actor)
        return notifications.deferred_mutation(
            result,
            # A pause or resume also moved the worker plans it carried.
            [plan_id, *result.get("cascaded_plan_ids", ()), *touched],
            db=db,
            message_floor=message_floor,
            artifact_event_floor=artifact_event_floor,
            drives=drives,
        )
    return notifications.DeferredMutation(result, ())



def _required_mutation_id(value: str) -> str:
    """Require a caller-owned durable idempotency key for every mutation."""
    if not isinstance(value, str) or not value.strip():
        raise service.PlanInputError("mutation_id is required")
    return value.strip()


_revalidate_actor = boundary_actor.revalidate_actor


def _authorize_and_dispatch(
    db, plan_id, operation, params, actor, provenance, plan, workflow_version, workflow_steps,
) -> dict[str, Any]:
    """Dispatch one freshly authorized mutation inside the outer write."""
    boundary_worker.authorize(db, plan, operation, params, actor)
    review_operation = operation in {"accept", "accept_with_follow_up", "reject"}
    # Naming the next task to start is not a review act, so a review that
    # names one also needs the authority to drive this plan's nodes.
    foreign_conductor_add = _authorize_thread_mutation_owner(
        db, plan, operation, params, actor,
        review_operation=review_operation and "next_node_id" not in params,
    )
    if foreign_conductor_add:
        provenance = "plan.conductor_add"
    if operation == "insert":
        _authorize_insert_target(db, plan_id, params)
    if operation in {"add", "insert"}:
        if "depends_on_node_id" in params:
            _authorize_prerequisite(db, plan_id, params, actor)
    elif operation not in {"pause", "resume"}:
        node = _authorize_node(db, plan, params, operation, by_user=actor.kind == "user")
        if operation in {"accept", "accept_with_follow_up", "reject"}:
            _authorize_reviewer(plan, node, actor)
        if blocked_wait.is_wait(operation, params):
            blocked_wait.authorize(plan, actor)
            blocked_wait.resolve_parent(db, plan, params)
        if scheduled_wait.is_scheduled(operation, params):
            scheduled_wait.authorize(plan, actor)
        if operation in {"arm_watch", "resolve_watch"} or blocked_wait.is_wait(operation, params):
            _authorize_plan_watch(node, plan, actor, params)
        if operation in {"add_dependency", "remove_dependency"}:
            _authorize_prerequisite(db, plan_id, params, actor)
    result = boundary_dispatch.dispatch(
        plan_id, operation, params, actor, provenance, db,
        workflow_version=workflow_version, workflow_steps=workflow_steps,
    )
    if isinstance(result, tuple):
        return {"node": node_projection(result[0]), "follow_up": node_projection(result[1])}
    if isinstance(result, dict):
        return result
    return {"node": node_projection(result)}


def _authorize_prerequisite(db, plan_id: str, params: dict[str, Any], actor: PlanActor) -> None:
    """Let a dependency name another plan's task only if the caller could read that plan.

    The read rule is ``_authorize_plan_scope``, the same one ``plan_read``
    applies (#1552). Its refusal is the dependency's own one message, so an
    edge attempt never tells the caller whether a task it may not read
    exists. A missing id is left to ``dependencies`` to refuse identically.
    An edge the caller already has can always be removed, even after its
    thread moved project: it discloses nothing the caller did not know.
    """
    import plans.dependencies as dependencies

    prerequisite_id = params.get("depends_on_node_id")
    row = db.execute(
        "SELECT plan_id FROM plan_nodes WHERE id=?", (prerequisite_id,),
    ).fetchone()
    if row is None or row["plan_id"] == plan_id:
        return
    if db.execute(
        "SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
        (params.get("node_id"), prerequisite_id),
    ).fetchone():
        return
    try:
        _authorize_plan_scope(db, row["plan_id"], actor)
    except PermissionError:
        raise service.PlanError(dependencies._DEPENDENCY_REFUSED) from None


def _authorize_plan_scope(db, plan_id: str, actor: PlanActor):
    """Resolve current accessible plan scope before a receipt can be disclosed."""
    plan = service._plan_row(db, plan_id)
    owner_kind, owner_id = plan["owner_kind"], plan["owner_id"]
    if owner_kind == "project_scope":
        if actor.kind == "thread":
            raise PermissionError("thread actors cannot mutate project scope plans")
        if actor.project_id != owner_id and not user_in_project(
            owner_id, actor.user_id, db=db,
        ):
            raise PermissionError("plan is outside the current project scope")
        return plan
    if owner_kind == "workspace_scope":
        if actor.kind == "thread":
            raise PermissionError("thread actors cannot mutate workspace scope plans")
        if owner_id != config.WORKSPACE_SELECTION.workspace_id:
            raise PermissionError("plan is outside the current workspace scope")
        return plan
    authorize_thread_owner(db, owner_id, actor)
    return plan


def authorize_thread_owner(db, owner_id: str, actor: PlanActor) -> None:
    """The one access rule for a thread's plan: same user and, for a thread, same project.

    Checked against the owner's current row on every call, so a thread that
    moves project moves its plan's readers with it. A thread that does not
    exist is refused like one the actor may not see, so a refusal never says
    which of the two it was.
    """
    owner = db.execute(
        "SELECT id, user_id, project_id FROM threads WHERE id=?", (owner_id,)
    ).fetchone()
    if owner is None or owner["user_id"] != actor.user_id:
        raise PermissionError("plan owner is not accessible to the current actor")
    if actor.kind == "thread" and owner["project_id"] != actor.project_id:
        if get_corsia_agent(db=db) == actor.thread_id:
            return
        raise PermissionError("plan is outside the current project scope")



def _authorize_thread_mutation_owner(
    db, plan, operation: str, params: dict[str, Any], actor: PlanActor, *, review_operation: bool,
) -> bool:
    """Require ownership, returning whether this is the one conductor foreign add."""
    if actor.kind != "thread" or plan["owner_kind"] != "thread" or plan["owner_id"] == actor.thread_id:
        return False
    if thread_may_mutate(db, plan, operation, params, actor):
        return True
    from prompts.plan_tools import FOREIGN_PLAN_MUTATION

    # A foreign add has one useful refusal no matter who made it. A conductor's
    # other foreign mutations take the same route; ordinary review retains its
    # existing reviewer-specific refusal below.
    if operation == "add" or conductor_may_add(db, plan, actor):
        raise errors.PlanRefusal(FOREIGN_PLAN_MUTATION)
    if not review_operation:
        raise PermissionError("thread plan mutations require the owning thread")
    return False


def _authorize_node(db, plan, params: dict[str, Any], operation: str, *, by_user: bool = False):
    """Require a node-targeted operation to target a node this boundary may drive.

    The checks are ``boundary_guard.drive_refusal``, the same guard a closing
    operation's named next task is judged by; here each refusal raises.
    """
    node_ids = params.get("node_ids")
    if isinstance(node_ids, list):
        # A bulk call is authorized as a whole on plan membership: one list must
        # not smuggle in a node from another plan, so that refuses everything
        # before any write. What would end one node but not another -- a lane
        # owning it, or an archive guard -- is reported per node by the
        # dispatch instead, so it never discards the rest of the cleanup.
        for target in node_ids:
            node = service._node_row(db, target)
            refusal = boundary_guard.drive_refusal(db, node, plan=plan, operation=operation)
            if refusal is not None and refusal.code == boundary_guard.OUTSIDE_PLAN:
                raise PermissionError("plan node is outside the requested plan")
        return None
    return _authorize_one_node(db, plan, params.get("node_id"), operation, by_user=by_user)


def _authorize_one_node(db, plan, node_id, operation: str, *, by_user: bool = False):
    """Judge one node against the drive guard, raising the matching refusal."""
    if not isinstance(node_id, str) or not node_id:
        raise service.PlanInputError("node_id is required")
    node = service._node_row(db, node_id)
    refusal = boundary_guard.drive_refusal(db, node, plan=plan, operation=operation, by_user=by_user)
    if refusal is None:
        return node
    if refusal.code == boundary_guard.OUTSIDE_PLAN:
        raise PermissionError("plan node is outside the requested plan")
    if refusal.code == boundary_guard.ARCHIVED:
        # Archive ends a node's mutable life, so one guard here covers every
        # node-targeted operation rather than each writer re-checking.
        raise service.PlanError(service.ARCHIVED_NODE)
    if refusal.code == boundary_guard.LANE_OWNED:
        # A node whose settlement a lane owns is read, positioned and extended
        # here like any other, but never driven or ended: its terminal state is
        # the record of an answer that lane holds.
        node_settlement.refuse(refusal.lane, operation)
    raise RuntimeError(f"unhandled node refusal: {refusal.code}")


#: A watch names a real wait, and a task waits before it starts and while its
#: review is outstanding as truthfully as it does mid-execution.
_WATCHABLE_STATES = frozenset({"pending", "active", "review_requested"})


def _authorize_plan_watch(node, plan, actor: PlanActor, params) -> None:
    """Bind watch operations to one active node and its current thread actor."""

    if actor.kind != "thread" or actor.thread_id is None:
        raise PermissionError("plan watches require a current thread actor")
    errors.require_node_revision(node, params["expected_revision"])
    owner_thread = plan["owner_id"] if plan["owner_kind"] == "thread" else None
    if owner_thread != actor.thread_id:
        raise PermissionError("plan watch requires the node's current actor")
    if node["state"] not in _WATCHABLE_STATES:
        raise service.PlanError("plan watches require an open node")


def _authorize_insert_target(db, plan_id: str, params: dict[str, Any]) -> None:
    """Keep optional insert parent and sibling targets inside the authorized plan."""
    for field in ("before_node_id", "parent_id"):
        target_id = params.get(field)
        if target_id is None:
            continue
        if not isinstance(target_id, str) or not target_id:
            raise service.PlanInputError(f"{field} must be a node id")
        if service._node_row(db, target_id)["plan_id"] != plan_id:
            raise PermissionError("insert target is outside the requested plan")


def _authorize_reviewer(plan, node, actor: PlanActor) -> None:
    """Permit an assigned human, the plan owner, or human fallback to review.

    Only a plan's owner runs its tasks, so every task in a thread plan is the
    owner's own work; a parent tier reviews its delegated step in its own plan.
    """
    if actor.kind == "user" or actor.thread_id == plan["owner_id"]:
        return
    raise PermissionError("root review requires the plan owner or human fallback")


def _authorize_user_task(actor: PlanActor) -> None:
    """Permit only the user to mark a row done, any row (``plans.user_settle``)."""
    if actor.kind != "user":
        raise PermissionError("only the user marks a row done")
