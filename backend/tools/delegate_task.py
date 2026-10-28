"""delegate_task: create a hidden worker, send it the task, and hand it a plan task.

Model resolution and the caller's context come from ``tools.delegation``,
looked up through the module so one patch point serves every tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import TYPE_CHECKING, Any

from core.thread_identity import thread_agent_identity
from infra.db_async import run_in_db
from infra.fresh_threads import create_visible_fresh_thread
from infra.send_service import send_thread_input
from infra.send_types import InboxQueuedInput
from llm.delegation_model import dispatch_scope, eligible_delegation_model
from subagents.cc_invocation import CONFINEMENT_NONE, CONFINEMENT_REVIEW, CONFINEMENTS
from tools import delegation
from tools.base import Tool

if TYPE_CHECKING:
    from subagents.cc_spawn import RuleStart

logger = logging.getLogger(__name__)

#: Shown only to a caller that is itself a delegated worker (decision 6c8347eb).
#: Guidance, never a refusal: any depth of delegation stays allowed.
WORKER_NOTE = (
    "You are a delegated worker yourself, so the brief you were given is still"
    " yours. Verify this worker's result when it returns and do the rest"
    " yourself; hand off only a separable part, never the whole brief."
)


class DelegateTaskTool(Tool):
    """Create a hidden delegated child, send it the initial task, and hand it a plan task."""

    @property
    def name(self) -> str:
        return "delegate_task"

    @property
    def description(self) -> str:
        return (
            "Delegate a self-contained task to a hidden child thread using the "
            "required explicit model. This is the atomic create-child-and-send-first-"
            "message workflow; use the smaller delegate_* tools when you need "
            "separate steps. The worker takes over your current plan task unless "
            "plan_node_id names another task or 'none'; the result's plan_task says "
            "which. Confinement 'review' is only for a worker that reads outside, untrusted "
            "content (a contributor's PR, inbound mail, web pages); it limits the worker to "
            "file reads in read_paths. Routine reviews of your own project's work use the "
            "default. The result is its final message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return delegation._tool_schema(
            {
                "task": {"type": "string", "description": "Task for the child thread"},
                "title": {
                    "type": ["string", "null"],
                    "description": "Optional child title",
                },
                "model": {
                    "type": "string",
                    "pattern": r"\S",
                    "description": (
                        "Required explicit model id; there is no system default. "
                        "claude-code/* uses Claude Code; other ids use delegated "
                        "thread send."
                    ),
                },
                "account_id": {"type": ["string", "null"], "description": "Optional permitted vendor account; separate from the exact model."},
                "provider": {
                    "type": ["string", "null"],
                    "description": "Optional provider prefix for shorthand model names.",
                },
                "plan_node_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Plan task the worker takes over; it leaves your slot and "
                        "its result returns through it. Default: your current task. "
                        "'none' keeps your task and returns the result as a chat message."
                    ),
                },
                "confinement": {
                    "type": ["string", "null"], "enum": [*CONFINEMENTS, None],
                    "description": (
                        "Default 'none': usual tools, also for routine reviews. 'review': "
                        "file reads in read_paths, only for outside, untrusted content; any model."
                    ),
                },
                "read_paths": {
                    "type": ["array", "null"], "items": {"type": "string"},
                    "description": (
                        "With confinement 'review': the directories the worker may "
                        "read. Each must be one you can read yourself."
                    ),
                },
            },
            ["task", "model"],
        )

    async def execute(self, task: str, **kwargs: Any) -> str:
        from plans import public

        context = delegation._require_context(kwargs)
        if isinstance(context, str):
            return context
        parent_thread_id, parent = context
        if not task.strip():
            return "Error: task is required"
        requested = kwargs.get("plan_node_id")
        try:
            step = await run_in_db(partial(
                public.choose_delegated_step, parent_thread_id, requested,
            ))
        except public.PlanInputError as exc:
            return f"Error: invalid request, fix and resend - {exc}"
        started = await _spawn(
            parent_thread_id, parent, with_task_line(task.strip(), step, parent_thread_id), kwargs,
        )
        if isinstance(started, str):
            return started
        started["plan_task"] = await _hand_over(step, started["thread_id"], parent, requested)
        # A persistent agent may carry a spawner and is not a worker; a row
        # missing a column answers None, so a running worker keeps its receipt.
        if thread_agent_identity(parent)[1]:
            started["worker_note"] = WORKER_NOTE
        return delegation._json(started)


def with_task_line(message: str, step, parent_thread_id: str) -> str:
    """Append the line naming the handed plan task, or return ``message`` unchanged.

    The task is named by reference, never copied: its intent can be revised
    after dispatch, so a copy goes stale and a read stays current. The task is
    in the parent's plan, so the worker reads it with the parent's ``thread_id``
    (same user and project is the whole access rule), and that read does not
    depend on the attach, so a failed attach leaves the line true.
    """
    if step is None:
        return message
    return (
        f'{message}\n\nYour plan task: "{step.title}" ({step.node_id}). Read its intent and '
        f"acceptance criteria with plan_read(thread_id='{parent_thread_id}', "
        f"node_id='{step.node_id}') before you start, and again if you're told it changed."
    )


async def _spawn(
    parent_thread_id: str, parent: dict[str, Any], task: str, kwargs: dict[str, Any],
    *, rule_start: RuleStart | None = None,
) -> dict[str, Any] | str:
    """Start the worker; its receipt, or an error string with nothing started.

    ``rule_start`` is a rule-started worker's (R23), never a caller's: it records the
    worker before its first turn, and a full Claude Code cap raises
    ``LiveSpawnCapExceeded`` so the rule's runner can queue the start again.
    """
    from infra.delegation import DelegationModelRequired, require_explicit_thread_model

    try:
        requested_model = require_explicit_thread_model(delegation._requested_model_id(
            kwargs.get("model"), kwargs.get("provider")
        ))
    except DelegationModelRequired as e:
        return f"Error: {e}"
    confined = _confinement_args(kwargs)
    if isinstance(confined, str):
        return confined
    confinement, read_paths = confined
    scope = dispatch_scope(kwargs, parent.get("project_id"))
    model, refusal = await run_in_db(partial(eligible_delegation_model, requested_model, **scope))
    if refusal:
        return f"Error: {refusal}"
    try:
        requested_is_cc = delegation._is_cc_model(model)
    except Exception as exc:
        return f"Error: requested model could not be resolved: {exc}"
    if not requested_is_cc and delegation._is_delisted(model):
        return f"Error: requested model {model} is retired or unavailable."
    if requested_is_cc:
        return await _spawn_claude_code_child(
            parent_thread_id=parent_thread_id,
            task=task,
            title=kwargs.get("title"),
            model_id=model, account_id=kwargs.get("account_id"),
            confinement=confinement, read_paths=read_paths, rule_start=rule_start,
        )
    if confinement == CONFINEMENT_REVIEW and rule_start is None:
        from llm.resolution import resolve_execution_target
        from subagents.cc_confinement import ConfinementRefused
        from subagents.codex_confinement import verify_version

        target = await run_in_db(resolve_execution_target, model)
        if target.harness == "codex":
            try:
                await verify_version()
            except ConfinementRefused as exc:
                return f"Error: {exc}"
    # Eligibility above is the sole catalog gate for all delegation paths.
    return await _spawn_native_child(parent_thread_id, parent, task, kwargs, model,
                                     confinement, read_paths, rule_start)


async def _spawn_native_child(
    parent_thread_id: str, parent: dict[str, Any], task: str, kwargs: dict[str, Any],
    model: str, confinement: str, read_paths: list[str], rule_start: RuleStart | None,
) -> dict[str, Any] | str:
    """Start a native or local worker (``review`` confined to its read paths when asked)."""
    confined_dirs = await _native_read_dirs(parent_thread_id, confinement, read_paths)
    if isinstance(confined_dirs, str):
        return confined_dirs
    child_title = kwargs.get("title") or task[:60] or "Subagent"
    try:
        thread_row = await create_visible_fresh_thread(
            confined_read_dirs=confined_dirs,
            user_id=parent["user_id"] or "",
            profile=parent.get("profile") or "personal",
            title=child_title,
            model=model,
            project_id=parent.get("project_id"),
            spawned_by=parent_thread_id,
            thread_kind="subagent",
            emit_spawn_chip=True,
            account_id=kwargs.get("account_id"), actor_thread_id=parent_thread_id,
        )
        if rule_start is not None:
            await rule_start(thread_row["id"])
        result = await send_thread_input(
            thread_row["id"],
            thread_row,
            task,
            user={"id": parent["user_id"] or ""},
            metadata={
                "sender_kind": "agent",
                "from_thread": parent_thread_id,
                "from_title": parent.get("title") or "",
            },
            native_mode="delegation",
        )
    except Exception as e:
        return f"Error delegating task: {e}"
    if isinstance(result, InboxQueuedInput):
        return delegation._queued_result(thread_row["id"], result)
    return {
        "ok": True,
        "thread_id": thread_row["id"],
        "model": model,
        "driver": result.driver,
        "duplicate": result.duplicate,
    }


async def _native_read_dirs(
    parent_thread_id: str, confinement: str, read_paths: list[str],
) -> tuple[str, ...] | str:
    """A confined worker's read dirs, checked as the parent's own reads.

    ``()`` when unconfined; every harness preserves the stored read boundary.
    """
    from infra.fresh_threads import resolve_spawn_working_dir
    from subagents.cc_confinement import ConfinementRefused, require_readable_dirs

    if confinement == CONFINEMENT_NONE:
        return ()
    working_dir, _project = await run_in_db(resolve_spawn_working_dir, parent_thread_id)
    try:
        return await asyncio.to_thread(require_readable_dirs, read_paths, working_dir)
    except ConfinementRefused as e:
        return f"Error: {e}"


def _confinement_args(kwargs: dict[str, Any]) -> tuple[str, list[str]] | str:
    """The requested confinement and read paths, or an error string."""
    confinement = kwargs.get("confinement") or CONFINEMENT_NONE
    read_paths = list(kwargs.get("read_paths") or [])
    if confinement not in CONFINEMENTS:
        return f"Error: confinement must be one of {', '.join(CONFINEMENTS)}."
    if (confinement == CONFINEMENT_REVIEW) != bool(read_paths):
        return (
            "Error: confinement 'review' needs read_paths, the directories the "
            "worker may read, and read_paths needs confinement 'review'."
        )
    return confinement, read_paths


async def _hand_over(
    step, child_thread_id: str, parent: dict[str, Any], requested, *, via: str = "delegate_task",
) -> dict:
    """Attach the worker to its task and say what happened.

    The worker already has its message, so no failure here may lose the
    receipt: any error becomes a ``plan_task`` note naming the worker to attach
    by hand. ``delegate_send`` reuses this for a worker it hands a named task.
    """
    from plans import public

    said = partial(
        public.delegated_plan_task, step, requested=requested,
        child_thread_id=child_thread_id,
    )
    if step is None:
        return said()
    try:
        deferred = await run_in_db(partial(
            public.hand_over_delegated_step, step, child_thread_id, parent, via=via,
        ))
    except Exception as exc:
        if not isinstance(exc, (public.PlanError, PermissionError)):
            logger.exception("%s hand-over to %s failed", via, child_thread_id)
        return said(error=str(exc) or type(exc).__name__)
    try:
        public.deliver_deferred(deferred)
        await public.drive_deferred(deferred)
    except Exception:
        logger.exception("%s hand-over wake for %s failed", via, child_thread_id)
    return said()


async def _spawn_claude_code_child(
    *,
    parent_thread_id: str,
    task: str,
    title: str | None,
    model_id: str | None,
    account_id: str | None = None,
    confinement: str = CONFINEMENT_NONE,
    read_paths: list[str] | None = None,
    rule_start: RuleStart | None = None,
) -> dict[str, Any] | str:
    from connections.models import get_connection
    from subagents.cc_confinement import ConfinementRefused, require_readable_dirs
    from infra.subagent_caps import LiveSpawnCapExceeded
    from subagents import cc_runtime
    from subagents.cc_runtime_helpers import CCWorkingDirMissing
    from infra.fresh_threads import resolve_spawn_working_dir

    conn = get_connection("claude-code")
    if not conn:
        return "Error: Claude Code connection is not configured."
    cfg = conn.get("config") or {}
    binary = cfg.get("binary", "claude")
    selected = model_id or cfg.get("model")
    if not selected:
        return "Error: Claude Code delegation requires an explicit or configured model."
    try:
        target = delegation._resolve_cc_target(str(selected))
    except Exception as exc:
        return f"Error: Claude Code delegation blocked: {exc}"
    if not target.available:
        return (
            "Error: Claude Code delegation blocked: "
            f"{target.catalog_id} is unavailable ({target.availability_reason})"
        )
    working_dir, project_id = resolve_spawn_working_dir(parent_thread_id)
    add_dirs: tuple[str, ...] = ()
    if confinement != CONFINEMENT_NONE:
        try:
            add_dirs = await asyncio.to_thread(require_readable_dirs, read_paths or [], working_dir)
        except ConfinementRefused as e:
            return f"Error: {e}"
    try:
        child_id = await cc_runtime.spawn(
            parent_thread_id,
            task,
            binary=binary,
            target=target,
            working_dir=working_dir,
            title=title,
            project_id=project_id, account_id=account_id,
            confinement=confinement, add_dirs=add_dirs, rule_start=rule_start,
        )
    except LiveSpawnCapExceeded as e:
        if rule_start is not None:
            raise
        return str(e)
    except CCWorkingDirMissing as e:
        return f"Cannot delegate to Claude Code: {e}"
    except Exception as e:
        return f"Error delegating to Claude Code: {e}"
    return {"ok": True, "thread_id": child_id, "model": target.catalog_id}
