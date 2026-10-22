"""delegate_send: send a message into another thread, optionally handing a reused worker a plan task.

The send itself goes through ``tools.delegation``, looked up through the module
so one patch point serves every tool.
"""

from __future__ import annotations

from functools import partial
import logging
from typing import Any

from infra.local_only import send_confirmation_error
from infra.send_service import SendRejected
from infra.send_types import InboxQueuedInput
import tools.delegation as delegation
from tools.base import Tool
from tools.delegate_task import with_task_line
from tools.delegation_send_context import fetch_send_target, native_send_scope_error


logger = logging.getLogger(__name__)


class DelegateSendTool(Tool):
    """Send a turn into another CORSIA thread as the current agent."""

    @property
    def name(self) -> str:
        return "delegate_send"

    @property
    def description(self) -> str:
        return (
            "Send a message into another CORSIA thread with verified current-thread "
            "attribution. This is the tool form of the delegation /send endpoint. "
            "To give a worker you started more work that returns through your plan, "
            "name the task with plan_node_id; without it nothing is attached and the "
            "worker's reply arrives as a chat message."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return delegation._tool_schema(
            {
                "thread_id": {"type": "string", "description": "Target thread id"},
                "message": {"type": "string", "description": "Message to send"},
                "intent": {
                    "type": ["string", "null"],
                    "enum": ["progress", "completion", None],
                    "description": (
                        "progress (default) is an ordinary durable message. "
                        "completion delivers this delegated child turn's final "
                        "result to its registered parent exactly once; it binds "
                        "automatically to the executing child turn, refused for a structured child (use review evidence)."
                    ),
                },
                "idempotency_key": {
                    "type": ["string", "null"],
                    "description": "Optional retry key for duplicate suppression",
                },
                "plan_node_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Plan task for a worker you started: a new task it takes over "
                        "(only when it holds no open task; the result returns through it), "
                        "or the task it already works, which answers any input it blocked "
                        "on you for. Default: none; the reply arrives as chat and leaves "
                        "any input it blocked on you for still open."
                    ),
                },
                "target_scope_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional scope assertion. Must equal the target thread's "
                        "scope id when supplied; it does not authorize cross-scope sends."
                    ),
                },
                "no_sensitive_content": {
                    "type": "boolean",
                    "description": "Confirm this cloud-bound send contains no sensitive content.",
                },
            },
            ["thread_id", "message"],
        )

    async def execute(self, thread_id: str, message: str, **kwargs: Any) -> str:
        context = delegation._require_context(kwargs)
        if isinstance(context, str):
            return context
        from_thread_id, current = context
        if not message.strip():
            return "Error: message is required"

        target_payload = await delegation.run_in_db(
            fetch_send_target, thread_id, current["user_id"],
            current.get("project_id") or None,
        )
        if not target_payload:
            return f"Error: Thread not found: {thread_id}"
        target = target_payload["thread_row"]
        scope_error = native_send_scope_error(
            current=current,
            target_payload=target_payload,
            from_thread_id=from_thread_id,
            target_thread_id=thread_id,
            requested_scope_id=kwargs.get("target_scope_id"),
            backend_bound=kwargs.get("_host_access_turn_identity") is not None,
        )
        if scope_error:
            return scope_error
        confirmation_error = await delegation.run_in_db(
            send_confirmation_error,
            current,
            target,
            kwargs.get("no_sensitive_content"),
        )
        if confirmation_error:
            return confirmation_error
        intent = delegation._clean(kwargs.get("intent")) or "progress"
        if intent not in ("progress", "completion"):
            return "Error: intent must be 'progress' or 'completion'"

        # Server-bind the executing child turn when this is a delegated child
        # sending to its own registered parent. Callers may not invent a
        # different terminal identity.
        child_turn_id = await delegation.run_in_db(
            delegation._resolve_child_turn_for_parent, from_thread_id, thread_id,
        )

        if intent == "completion":
            return await delegation._send_explicit_completion(
                from_thread_id, thread_id, child_turn_id, message.strip(),
            )
        from plans import public

        requested = kwargs.get("plan_node_id")
        try:
            step = await delegation.run_in_db(partial(
                public.choose_reused_worker_step, from_thread_id, thread_id, requested,
            ))
        except public.PlanInputError as exc:
            return f"Error: invalid request, fix and resend - {exc}"
        plan_task = await _prepare_plan_task(
            step, thread_id, from_thread_id, current, requested,
        )
        if isinstance(plan_task, str):
            return plan_task
        text = with_task_line(message.strip(), step, from_thread_id)
        sent = await _send(thread_id, target, text, from_thread_id, current, kwargs)
        if isinstance(sent, str):
            return sent
        if step is not None and step.already_attached:
            sent.update(await _answer_needs(from_thread_id, thread_id, step.node_id, kwargs))
        if plan_task is not None:
            sent["plan_task"] = plan_task
        return delegation._json(sent)


async def _answer_needs(from_thread_id: str, thread_id: str, node_id: str, kwargs) -> dict:
    """Clear the worker's parent needs; the message is already delivered either way."""
    from plans import public

    try:
        cleared = await delegation.run_in_db(partial(
            public.answer_worker_needs, from_thread_id, thread_id, node_id,
            mutation_id=kwargs.get("idempotency_key"),
        ))
    except Exception:
        logger.exception("answer_worker_needs failed after delivery to %s", thread_id)
        return {
            "needs_cleared": 0,
            "needs_note": "Delivered, but recording the answer failed, so the worker's input "
            "is still open. Don't repeat the message; a short follow-up naming this "
            "plan_node_id clears it.",
        }
    return {"needs_cleared": cleared}


async def _prepare_plan_task(step, thread_id: str, from_thread_id: str, current, requested):
    """Attach the named task before delivery, or describe an unattached send."""
    from plans import public

    if step is None:
        note = await delegation.run_in_db(
            public.reused_worker_note, from_thread_id, thread_id,
        )
        return None if note is None else {"attached": False, "note": note}
    if not step.already_attached:
        try:
            deferred = await delegation.run_in_db(partial(
                public.hand_over_delegated_step,
                step,
                thread_id,
                current,
                via="delegate_send",
            ))
        except Exception as exc:
            if not isinstance(exc, (public.PlanError, PermissionError)):
                logger.exception("delegate_send hand-over to %s failed", thread_id)
            return f"Error: invalid request, fix and resend - {exc}"
        try:
            public.deliver_deferred(deferred)
            await public.drive_deferred(deferred)
        except Exception:
            logger.exception("delegate_send hand-over wake for %s failed", thread_id)
    return public.delegated_plan_task(
        step, requested=requested, child_thread_id=thread_id,
    )


async def _send(
    thread_id: str, target, text: str, from_thread_id: str, current, kwargs,
) -> dict[str, Any] | str:
    """Deliver one progress message; its receipt, or an error string."""
    from infra.execution_class import is_subagent

    metadata = {
        "sender_kind": "agent",
        "from_thread": from_thread_id,
        "from_title": current.get("title") or "",
        "scope": current.get("project_id") or "workspace",
    }
    target_kind = "subagent" if is_subagent(target) else "chat"
    try:
        result = await delegation.send_thread_input(
            thread_id,
            target,
            text,
            user={"id": current["user_id"] or ""},
            metadata=metadata,
            idempotency_key=kwargs.get("idempotency_key"),
            native_mode="delegation",
            accepted_effects={
                "send_ref": {
                    "sender_thread_id": from_thread_id,
                    "target_title": target.get("title") or "Untitled thread",
                    "target_kind": target_kind,
                }
            },
        )
    except SendRejected as exc:
        return f"Error: {exc.detail}"
    if isinstance(result, InboxQueuedInput):
        return delegation._queued_result(thread_id, result)
    return {
        "ok": True, "thread_id": thread_id,
        "driver": result.driver, "duplicate": result.duplicate,
    }
