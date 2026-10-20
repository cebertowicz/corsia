"""delegate_task: create a hidden worker, send it the task, and hand it a plan task.

Model resolution and the caller's context come from ``tools.delegation``,
looked up through the module so one patch point serves every tool.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

from core.thread_identity import thread_agent_identity
from infra.db_async import run_in_db
from infra.fresh_threads import create_visible_fresh_thread
from infra.send_service import send_thread_input
from infra.send_types import InboxQueuedInput
from llm.delegation_model import dispatch_scope, eligible_delegation_model
from tools import delegation
from tools.base import Tool

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
            "which."
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
        started = await _spawn(parent_thread_id, parent, task.strip(), kwargs)
        if isinstance(started, str):
            return started
        started["plan_task"] = await _hand_over(step, started["thread_id"], parent, requested)
        # A persistent agent may carry a spawner and is not a worker; a row
        # missing a column answers None, so a running worker keeps its receipt.
        if thread_agent_identity(parent)[1]:
            started["worker_note"] = WORKER_NOTE
        return delegation._json(started)


async def _spawn(
    parent_thread_id: str, parent: dict[str, Any], task: str, kwargs: dict[str, Any],
) -> dict[str, Any] | str:
    """Start the worker; its receipt, or an error string with nothing started."""
    from infra.delegation import DelegationModelRequired, require_explicit_thread_model

    try:
        requested_model = require_explicit_thread_model(delegation._requested_model_id(
            kwargs.get("model"), kwargs.get("provider")
        ))
    except DelegationModelRequired as e:
        return f"Error: {e}"
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
        )
    # Eligibility above is the sole catalog gate for all delegation paths.

    child_title = kwargs.get("title") or task[:60] or "Subagent"
    try:
        thread_row = await create_visible_fresh_thread(
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
) -> dict[str, Any] | str:
    from connections.models import get_connection
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
    try:
        child_id = await cc_runtime.spawn(
            parent_thread_id,
            task,
            binary=binary,
            target=target,
            working_dir=working_dir,
            title=title,
            project_id=project_id, account_id=account_id,
        )
    except LiveSpawnCapExceeded as e:
        return str(e)
    except CCWorkingDirMissing as e:
        return f"Cannot delegate to Claude Code: {e}"
    except Exception as e:
        return f"Error delegating to Claude Code: {e}"
    return {"ok": True, "thread_id": child_id, "model": target.catalog_id}
