"""Actor-bound current-plan read, ensure, update, and decision tools."""

from __future__ import annotations

import json
import time
from functools import partial
from typing import Any

from core.lesson_delivery import attach_plan_result
from infra.database import get_db
from infra.db_async import run_in_db
from infra.state.context import running_call_ids
from memory import node_start_recall
from plans import public
from plans.public import (
    read_current_status, PlanActor, ensure_current_plan, mutate, read_current_plan,
)
from prompts.plan_tools import CLOSE_NEXT_DESCRIPTION, USER_TASKS_DESCRIPTION
from tools.base import Tool
from tools.plan_read_events import emit_read
from tools.plan_schemas import (
    READ_PARAMS, THREAD_PARAM, decision_schema, published, update_schema,
)


def _require_context(kwargs: dict[str, Any]):
    """Reuse the established task-local session resolver only when executing."""
    from tools.delegation import _require_context as require_context

    return require_context(kwargs)


def _tool_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build the standard closed object schema without importing delegation startup code."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": properties, "required": required,
    }


#: Author-side decision operations. The user's answer is a human HTTP action,
#: never an agent tool call.
_DECISION_OPERATIONS = {
    "draft": public.draft_decision,
    "update": public.update_decision,
    "reorder": public.reorder_decision,
    "parent_review": public.parent_review_decision,
    "raise": public.raise_decision,
    "invalidate": public.invalidate_decision,
}

_OPERATIONS = [
    "add", "insert", "edit", "split", "reorder", "activate", "start",
    "resume_node", "block_node", "resolve_blocker",
    "request_review", "mark_done", "reject", "fail", "attach_spawned_thread",
    "replace_spawned_thread", "accept",
    "accept_with_follow_up", "arm_watch", "resolve_watch", "add_dependency",
    "remove_dependency", "add_follow_up", "add_correction", "close_node",
    "archive_node",
]


def _thread_actor(thread_id, actor_row, turn_identity=None, call_ids=None) -> PlanActor:
    """Build one current execution actor from server-bound task context."""

    return PlanActor(
        thread_id=thread_id,
        user_id=actor_row["user_id"],
        project_id=actor_row["project_id"],
        turn_id=getattr(turn_identity, "turn_id", None),
        **(call_ids or {}),
    )


def _current_operation(thread_id, actor_row, ensure, read: dict, reader):
    """Read/ensure the actor's plan, or read another thread's without creating it."""

    db = get_db()
    try:
        actor = _thread_actor(thread_id, actor_row)
        other = read.get("thread_id")
        other = other if other not in (None, thread_id) else None
        known = read.get("known_revision")
        include_folded = read.get("include_folded")
        if read.get("mode") == "full" and ensure:
            return ensure_current_plan(
                actor=actor, cursor=read.get("cursor"), known_revision=known,
                include_folded=include_folded, db=db,
            )
        if read.get("mode") == "full":
            page = read_current_plan(
                actor=actor, cursor=read.get("cursor"), known_revision=known,
                include_folded=include_folded, thread_id=other, db=db,
            )
        else:
            if ensure:
                ensure_current_plan(actor=actor, db=db)
            page = read_current_status(
                actor=actor, section=read.get("section"), cursor=read.get("cursor"),
                node_id=read.get("node_id"), known_revision=known,
                include_folded=include_folded, thread_id=other, db=db,
            )
        if other is not None:
            from infra.thread_confinement import read_restriction

            restriction = read_restriction(db, reader, other)
            if restriction is not None:
                return restriction.payload()
            return public.addressed_to(page, other, db=db)
        return page
    finally:
        db.close()


async def _execute_current(kwargs: dict[str, Any], *, ensure: bool) -> str:
    """Read or ensure the plan bound to the current execution actor."""

    context = _require_context(kwargs)
    if isinstance(context, str):
        return context
    thread_id, actor_row = context
    keys = (
        "mode", "node_id", "section", "cursor", "known_revision", "include_folded",
        "thread_id",
    )
    read = {key: kwargs.get(key) for key in keys}
    foreign = read["thread_id"] not in (None, thread_id)
    started = time.monotonic()
    from infra.thread_confinement import Reader

    try:
        result = await run_in_db(partial(
            _current_operation, thread_id, actor_row, ensure, read, Reader.from_tool(kwargs)
        ))
        reply = json.dumps(result, sort_keys=True)
    except PermissionError:
        if foreign:
            return "Error: that thread's plan is outside your project"
        return "Error: current plan is outside the execution actor scope"
    except public.PlanError as exc:
        return f"Error: {exc}"
    emit_read(thread_id, read, foreign, len(reply.encode()), started)
    return reply


class PlanReadTool(Tool):
    """Read the structured plan bound to the current execution actor."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "plan_read"

    @property
    def description(self) -> str:
        return (
            "Read your plan: status by default, node_id for one node, "
            "mode='full' for all nodes without bodies."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema({**READ_PARAMS, **THREAD_PARAM}, required=[])

    async def execute(self, **kwargs: Any) -> str:
        """Return the current plan's status (or full plan) or null when not ensured."""
        return await _execute_current(kwargs, ensure=False)


class PlanEnsureTool(Tool):
    """Ensure the single structured plan bound to the current execution actor."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "plan_ensure"

    @property
    def description(self) -> str:
        return (
            "Ensure the current actor's plan exists, then read it as plan_read."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _tool_schema(READ_PARAMS, required=[])

    async def execute(self, **kwargs: Any) -> str:
        """Ensure the current structured plan and return its status (or full plan)."""
        return await _execute_current(kwargs, ensure=True)


def _mutation_operation(
    thread_id, actor_row, turn_identity, plan_id, operation, params, mutation_id, workflows,
    call_ids=None,
):
    """Execute one actor-bound mutation on a worker database connection."""

    db = get_db()
    try:
        import tools.workflow_task_fields as workflow_task_fields
        actor = _thread_actor(thread_id, actor_row, turn_identity, call_ids)
        workflow_version = None
        if operation in {"add", "insert"}:
            workflow_task_fields.validate_creation_reference(params, workflows)
            workflow_version = workflow_task_fields.creation_version(params, workflows)
        result = mutate(
            plan_id,
            operation,
            params,
            actor=actor,
            provenance="tool.plans",
            mutation_id=mutation_id,
            db=db,
            workflow_version=workflow_version,
        )
        if operation in {"add", "insert"}:
            workflow_task_fields.append_add_notice(result.result, db, workflows)
        elif operation in {"accept", "accept_with_follow_up", "fail", "close_node"}:
            workflow_task_fields.append_close_notice(
                result.result, node_id=params["node_id"], db=db, workflows=workflows,
            )
        attach_plan_result(db, result, thread_id, operation in node_start_recall.NODE_OPERATIONS)
        node_start_recall.attach_to_result(db, operation, result, caller_thread_id=thread_id)
        return result
    finally:
        db.close()


def _decision_operation(thread_id, actor_row, operation, plan_id, params, mutation_id):
    """Execute one actor-bound decision operation on a worker connection."""

    db = get_db()
    call = _DECISION_OPERATIONS[operation]
    common = {
        "mutation_id": mutation_id,
        "actor": _thread_actor(thread_id, actor_row),
        "provenance": "tool.plans.decision",
        "db": db,
    }
    try:
        if operation == "draft":
            return call(plan_id, **common, **params)
        result = call(params.pop("decision_id", None), **common, **params)
        attach_plan_result(db, result, thread_id, operation in node_start_recall.NODE_OPERATIONS)
        node_start_recall.attach_to_result(db, operation, result, caller_thread_id=thread_id)
        return result
    finally:
        db.close()


class _OperationTool(Tool):
    """A plan tool that validates against its full per-operation contract.

    ``parameters`` is what a model is shown (``plan_schemas.published``);
    ``contract`` is what a call must satisfy. Both come from one builder.
    """

    @property
    def contract(self) -> dict[str, Any]:
        """The full schema, per-operation rules included."""
        raise NotImplementedError

    @property
    def parameters(self) -> dict[str, Any]:
        """The schema a model is shown."""
        return published(self.contract)

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Validate against the contract, not the published schema."""
        return self._validate(params, self.contract, "")


class PlanDecisionTool(_OperationTool):
    """Author, review, order, and raise structured decisions for the user."""

    @property
    def name(self) -> str:
        return "plan_decision"

    @property
    def description(self) -> str:
        return (
            "Author structured plan decisions for the user. Draft with context, "
            "tradeoff, recommendation, rationale and 2-4 A/B/C/D options; the "
            "parent tier reviews before raise. pick \"any\" is for independent items; "
            "tradeoffs get their own decision. Reuse one mutation_id per logical "
            "action and pass the decision's expected_revision on every change."
        )

    @property
    def contract(self) -> dict[str, Any]:
        return decision_schema(_DECISION_OPERATIONS)

    async def execute(self, **kwargs: Any) -> str:
        """Apply one authorized decision operation and return its projection."""
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor_row = context
        operation = kwargs["operation"]
        if operation not in _DECISION_OPERATIONS:
            return f"Error: unsupported decision operation: {operation}"
        params = dict(kwargs.get("params") or {})
        try:
            result = await run_in_db(partial(
                _decision_operation, actor_thread_id, actor_row, operation,
                kwargs["plan_id"], params, kwargs["mutation_id"],
            ))
            projected = public.deliver_deferred(result)
            await public.drive_deferred(result)
            return json.dumps(projected, sort_keys=True)
        except PermissionError:
            return "Error: plan decision is outside the current actor scope"
        except public.PlanError as exc:
            return f"Error: {exc}"
        except TypeError as exc:
            return f"Error: invalid decision parameters: {type(exc).__name__}"


class PlanUpdateTool(_OperationTool):
    """Update only the structured plan authorized for the current actor."""

    self_describing_errors = True

    @property
    def name(self) -> str:
        return "plan_update"

    @property
    def description(self) -> str:
        return (
            "Update plan. Retry: mutation_id. Defaults: kind=task; verification=passed. "
            "{'plan_id': 'plan-id', 'mutation_id': 'arm-watch-1', "
            "'operation': 'arm_watch', 'params': {'node_id': 'node-id', "
            "'expected_revision': 0, 'condition': {'type': 'deadline', "
            "'value': '2030-01-01T00:00:00+00:00'}, 'expected_event': "
            "'external.done'}}. Pause is the user's; to set a task aside, "
            "arm_watch a deadline with reason instead. "
            + CLOSE_NEXT_DESCRIPTION
            + " "
            + USER_TASKS_DESCRIPTION
        )

    @property
    def contract(self) -> dict[str, Any]:
        return update_schema(_OPERATIONS)

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Answer a pause, which the schema no longer offers, with its replacement."""
        if params.get("operation") in public.PAUSE_OPERATIONS:
            node_id = (params.get("params") or {}).get("node_id")
            return [public.pause_refusal(node_id if isinstance(node_id, str) else None)]
        return super().validate_params(params)

    async def execute(self, **kwargs: Any) -> str:
        """Apply one authorized plan mutation and return its durable projection."""
        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        actor_thread_id, actor_row = context
        plan_id, mutation_id = kwargs["plan_id"], kwargs["mutation_id"]
        operation, params = kwargs["operation"], kwargs["params"]
        turn_identity = kwargs.get("_host_access_turn_identity")

        try:
            import tools.workflow_task_fields as workflow_task_fields

            workflows = await workflow_task_fields.operation_workflows(
                _thread_actor(actor_thread_id, actor_row, turn_identity), operation, params,
            )
            result = await run_in_db(partial(
                _mutation_operation,
                actor_thread_id, actor_row, turn_identity, plan_id, operation,
                params, mutation_id, workflows, running_call_ids(),
            ))
            projected = public.deliver_deferred(result)
            await public.drive_deferred(result)
            return json.dumps(projected, sort_keys=True)
        except PermissionError:
            return "Error: plan is outside the current actor scope"
        except public.PlanInputError as exc:
            # Distinct from a conflict on purpose: an agent that re-reads and
            # retries a malformed body loops forever. Naming the class tells it
            # to fix the request instead.
            return f"Error: invalid request, fix and resend - {exc}"
        except public.PlanError as exc:
            if isinstance(exc, public.RevisionConflict):
                return f"Error: conflict, re-read the plan and retry - {exc}"
            # The plan is current; the node's state refuses this operation, so
            # a re-read and identical retry would refuse again.
            return f"Error: state precondition, change the request - {exc}"
